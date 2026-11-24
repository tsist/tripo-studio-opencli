// Private stdin entry point for the Python driver. The driver owns the shared
// browser lock and the receiver/Studio preflight while this process is running.
import { spawnSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { existsSync, linkSync, readFileSync, realpathSync, unlinkSync, writeFileSync } from 'node:fs';
import { isAbsolute, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { BridgeUI, bindExistingBridgePage, classifyBridgeExportCaptures } from './tripo_bridge_ui.mjs';

const VERIFIED_OPENCLI = '1.8.7';
const CAPTURE_WINDOW_MS = 45000;
const EXPORT_ENDPOINT = '/v2/studio/operation/export';
const EXPORT_ENDPOINTS = new Set([EXPORT_ENDPOINT, '/v2/studio/operation/export-public-model']);
const UUID = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/;
const ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
const receiverScript = fileURLToPath(new URL('./tripo_bridge_receiver.py', import.meta.url));
const ledgerScript = fileURLToPath(new URL('./tripo_ledger.py', import.meta.url));

class BridgeCommandError extends Error {
  constructor(code, message) {
    super(message);
    this.code = code;
  }
}

function requireThat(ok, code, message) {
  if (!ok) throw new BridgeCommandError(code, message);
}

function contextOf(request) {
  return { profile: request.profile, session: request.session, page: request.pageId, project_id: request.projectId };
}

function sameContext(actual, expected) {
  return Object.entries(expected).every(([key, value]) => actual?.[key] === value);
}

function requireRequest(request) {
  requireThat(request?.mode === 'send' || request?.mode === 'receipt',
    'INVALID_BRIDGE_REQUEST', 'Choose send or receipt.');
  for (const key of ['profile', 'session', 'pageId', 'taskId', 'stepKey']) {
    requireThat(typeof request[key] === 'string' && ID.test(request[key]),
      'INVALID_BRIDGE_REQUEST', `A valid ${key} is required.`);
  }
  requireThat(typeof request.projectId === 'string' && UUID.test(request.projectId),
    'INVALID_BRIDGE_REQUEST', 'A valid Studio project UUID is required.');
  const paths = ['ledger', 'python', 'ledgerScript', 'receiptFile'];
  if (request.mode !== 'receipt' || request.localReceiptOnly !== true) paths.push('opencliRoot');
  for (const key of paths) {
    requireThat(typeof request[key] === 'string' && isAbsolute(request[key]),
      'INVALID_BRIDGE_REQUEST', `An absolute ${key} path is required.`);
  }
}

function readIntent(request, task) {
  const step = Object.hasOwn(task.steps ?? {}, request.stepKey) ? task.steps[request.stepKey] : null;
  const context = contextOf(request);
  const source = step?.metadata?.bridge_source;
  const plan = {
    project_id: request.projectId,
    model_format: 'fbx',
    fbx_preset: 'blender',
    texture_size: 8192,
    texture_packaging: 'embedded',
  };
  requireThat(task.task_id === request.taskId && task.schema_version >= 2 && task.region === 'cn' &&
    step?.operation === 'export' && step.credits === 0 && step.downloads === 1 &&
    sameContext(step.metadata?.bridge_context, context) &&
    Object.entries(plan).every(([key, value]) => step.metadata?.bridge_plan?.[key] === value) &&
    source?.project_id === request.projectId && source.owner_verified === true &&
    source.net_credits === 0 && typeof source.operator_id === 'string' && UUID.test(source.operator_id),
  'BRIDGE_INTENT_MISMATCH', 'This ledger step does not match the project, variant, browser and export plan.');
  return {
    step, context, plan, source,
    identity: {
      schema_version: 1, task_id: request.taskId, step_key: request.stepKey,
      browser_context: context, source_operator_id: source.operator_id,
    },
  };
}

function pythonJson(python, argv, input) {
  const child = spawnSync(python, ['-X', 'utf8', ...argv], {
    input, encoding: 'utf8', timeout: 35000, maxBuffer: 4 * 1024 * 1024, windowsHide: true,
  });
  if (child.status !== 0) return null;
  const result = parseJson(child.stdout);
  return result?.ok === true ? result : null;
}

function ledgerFor(request) {
  requireThat(realpathSync(request.ledgerScript) === realpathSync(ledgerScript),
    'INVALID_BRIDGE_REQUEST', 'Use the ledger shipped with this Studio driver.');
  return (ledger, ...argv) => {
    const result = pythonJson(request.python, [ledgerScript, '--ledger', ledger, ...argv]);
    requireThat(result, 'BRIDGE_LEDGER_ERROR', 'The Studio ledger did not accept the requested read or transition.');
    return result;
  };
}

function checkReceiver(request, expectedBinding) {
  const check = request.receiverCheck;
  const binding = check?.binding;
  requireThat(expectedBinding && binding &&
    ['pid', 'mcp_port', 'work_file', 'scene', 'checkpoint'].every(
      key => binding[key] !== undefined && expectedBinding[key] === binding[key]),
  'BRIDGE_RECEIVER_MISMATCH', 'The receiver check does not target the receiver recorded in this ledger step.');
  const result = pythonJson(request.python, [receiverScript], JSON.stringify(check));
  requireThat(result, 'BRIDGE_RECEIVER_CHANGED', 'The specified Blender receiver did not pass the last check before sending.');
}

async function loadOpenCLI(root) {
  try {
    const info = JSON.parse(readFileSync(join(root, 'package.json'), 'utf8'));
    requireThat(info.name === '@jackwener/opencli' && info.version === VERIFIED_OPENCLI,
      'UNVERIFIED_OPENCLI', `Official Bridge requires OpenCLI ${VERIFIED_OPENCLI} for pinned capture readback.`);
    const { Page } = await import(pathToFileURL(join(root, 'dist/src/browser/page.js')));
    const { sendCommand } = await import(pathToFileURL(join(root, 'dist/src/browser/daemon-client.js')));
    requireThat(typeof Page === 'function' && typeof sendCommand === 'function',
      'UNVERIFIED_OPENCLI', 'The installed OpenCLI browser and capture APIs are missing.');
    return { Page, sendCommand };
  } catch (error) {
    if (error instanceof BridgeCommandError) throw error;
    throw new BridgeCommandError('UNVERIFIED_OPENCLI', 'The verified OpenCLI package could not be loaded.');
  }
}

function parseJson(value) {
  if (typeof value !== 'string') return null;
  try { return JSON.parse(value); } catch { return null; }
}

function writeOnce(destination, value) {
  const temporary = `${destination}.${randomUUID()}.tmp`;
  writeFileSync(temporary, JSON.stringify(value) + '\n', { flag: 'wx', flush: true });
  try {
    linkSync(temporary, destination);
  } finally {
    unlinkSync(temporary);
  }
}

function readRecord(filename) {
  const record = parseJson(readFileSync(filename, 'utf8'));
  requireThat(record, 'INVALID_BRIDGE_RECORD', 'The existing Bridge evidence record is unreadable.');
  return record;
}

function matchingAttempt(record, identity) {
  return sameContext(record.browser_context, identity.browser_context) &&
    ['schema_version', 'task_id', 'step_key', 'source_operator_id'].every(
      key => record[key] === identity[key]) &&
    Number.isSafeInteger(record.capture_not_before_ms) && record.capture_not_before_ms > 0;
}

function sameTimeWindow(timestamp, start) {
  return Number.isSafeInteger(timestamp) && timestamp >= start && timestamp <= start + CAPTURE_WINDOW_MS;
}

function enforceSource(snapshot, intent) {
  requireThat(snapshot?.source?.project_id === intent.plan.project_id &&
    snapshot.source.ready === true && snapshot.source.readonly === false &&
    snapshot.source.operator_id === intent.source.operator_id,
  'BRIDGE_SOURCE_CHANGED', 'The specified Studio variant is not ready in the bound tab.');
}

function buildExportReceipt(rows, attempt) {
  let classified;
  try {
    classified = classifyBridgeExportCaptures(rows, attempt.browser_context.project_id);
  } catch (error) {
    // This classifier emits only fixed messages for its known validation failures.
    throw new BridgeCommandError('BRIDGE_CAPTURE_REJECTED', error.message);
  }
  if (!classified.receipt) return { post_requests: classified.post_requests, receipt: null };
  const row = classified.receipt;
  requireThat(UUID.test(row.response.operator_id) &&
    (!Object.hasOwn(row.request, 'operator_id') || row.request.operator_id === attempt.source_operator_id),
  'BRIDGE_CAPTURE_REJECTED', 'The export receipt names an invalid export or a different Studio variant.');
  return {
    post_requests: classified.post_requests,
    receipt: {
      endpoint: EXPORT_ENDPOINT, request_id: row.request_id, timestamp: row.timestamp,
      http: 200, code: 0, export_operator_id: row.response.operator_id,
      source_operator_id: attempt.source_operator_id, project_id: attempt.browser_context.project_id,
      format: 'fbx', fbx_preset: 'blender', texture_size: 8192, texture_packaging: 'embedded',
      with_animation: false, enable_bake_animation: false,
    },
  };
}

export function classifyAttemptCapture(rows, attempt) {
  requireThat(Array.isArray(rows), 'BRIDGE_CAPTURE_UNAVAILABLE', 'OpenCLI did not return capture rows.');
  const relevant = [];
  const posts = new Map();
  for (const captured of rows) {
    let url;
    try { url = new URL(captured.url); } catch { continue; }
    if (url.origin !== 'https://api-cn.tripo3d.com' || !EXPORT_ENDPOINTS.has(url.pathname)) continue;
    requireThat(Number.isSafeInteger(captured.timestamp),
      'BRIDGE_CAPTURE_REJECTED', 'An export request has no timestamp to associate it with this attempt.');
    if (!sameTimeWindow(captured.timestamp, attempt.capture_not_before_ms)) continue;
    const request = parseJson(captured.requestBodyPreview);
    const response = parseJson(captured.responsePreview);
    const row = {
      endpoint: url.pathname,
      request_id: typeof captured.requestId === 'string' && ID.test(captured.requestId) ? captured.requestId : null,
      method: captured.method,
      timestamp: captured.timestamp,
      http: captured.responseStatus,
      capture_complete: captured.captureComplete === true,
      request: request && typeof request === 'object' ? request : {},
      response: { code: response?.code, operator_id: response?.data?.operator_id ?? response?.operator_id },
    };
    if (row.method !== 'POST' || !row.request_id) {
      relevant.push(row);
      continue;
    }
    requireThat((!Object.hasOwn(row.request, 'project_id') || row.request.project_id === attempt.browser_context.project_id) &&
      (!Object.hasOwn(row.request, 'operator_id') || row.request.operator_id === attempt.source_operator_id),
    'BRIDGE_CAPTURE_REJECTED', 'The observed POST names a different project or selected Studio variant.');
    const previous = posts.get(row.request_id);
    requireThat(!previous || sameRequest(previous, row),
      'BRIDGE_CAPTURE_REJECTED', 'The same OpenCLI request ID was observed with different export identities.');
    if (previous?.capture_complete && row.capture_complete) {
      requireThat(previous.http === row.http && previous.response.code === row.response.code &&
        previous.response.operator_id === row.response.operator_id &&
        ['project_id', 'operator_id', 'format', 'fbx_preset', 'texture_size', 'texture_packaging', 'with_animation', 'enable_bake_animation'].every(
          key => previous.request[key] === row.request[key]),
      'BRIDGE_CAPTURE_REJECTED', 'The same OpenCLI request ID has contradictory completed receipts.');
    }
    if (!previous?.capture_complete || row.capture_complete) posts.set(row.request_id, row);
  }
  const result = buildExportReceipt([...relevant, ...posts.values()], attempt);
  const observed = posts.values().next().value;
  const request = observed ? { endpoint: observed.endpoint, request_id: observed.request_id, timestamp: observed.timestamp } : null;
  return { ...result, request };
}

function sameRequest(left, right) {
  return ['endpoint', 'request_id', 'timestamp'].every(key => left[key] === right[key]);
}

function rememberRequest(filename, attempt, request) {
  if (existsSync(filename)) {
    const saved = readRecord(filename);
    requireThat(matchingAttempt(saved, attempt) && saved.capture_not_before_ms === attempt.capture_not_before_ms &&
      saved.request && EXPORT_ENDPOINTS.has(saved.request.endpoint) && typeof saved.request.request_id === 'string' &&
      ID.test(saved.request.request_id) && sameTimeWindow(saved.request.timestamp, attempt.capture_not_before_ms),
    'BRIDGE_CAPTURE_REJECTED', 'The saved export request cannot be associated with this attempt.');
    requireThat(!request || sameRequest(saved.request, request),
      'BRIDGE_CAPTURE_REJECTED', 'A different export request appeared after the first POST was recorded.');
  } else if (request) {
    writeOnce(filename, { ...attempt, request });
  }
}

function recoverReceipt(filename, attempt, knownOperator) {
  const record = readRecord(filename);
  const saved = record.export_receipt;
  requireThat(matchingAttempt(record, attempt) && record.capture_not_before_ms === attempt.capture_not_before_ms &&
    saved?.project_id === attempt.browser_context.project_id &&
    saved.source_operator_id === attempt.source_operator_id &&
    (!knownOperator || saved.export_operator_id === knownOperator) &&
    sameTimeWindow(saved.timestamp, attempt.capture_not_before_ms) &&
    saved.endpoint === EXPORT_ENDPOINT && typeof saved.request_id === 'string' && ID.test(saved.request_id),
  'BRIDGE_RECEIPT_MISMATCH', 'The saved receipt does not belong to this Bridge attempt.');
  return buildExportReceipt([{
    endpoint: saved.endpoint, method: 'POST', request_id: saved.request_id, timestamp: saved.timestamp,
    http: saved.http, capture_complete: true, request: saved,
    response: { code: saved.code, operator_id: saved.export_operator_id },
  }], attempt).receipt;
}

function output(status, context, fields = {}) {
  return { ok: true, status, browser_context: context, receiver_verified: false, ...fields };
}

export async function runBridge(request, dependencies = {}) {
  requireRequest(request);
  const runLedger = dependencies.runLedger ?? ledgerFor(request);
  const ledgerArgs = ['--task-id', request.taskId];
  const task = runLedger(request.ledger, 'status', ...ledgerArgs);
  const intent = readIntent(request, task);
  const attemptFile = `${request.receiptFile}.attempt`;
  const requestFile = `${request.receiptFile}.request`;
  const conflictFile = `${request.receiptFile}.conflict`;
  const commit = (status, evidence, ...argv) => runLedger(
    request.ledger, 'commit', ...ledgerArgs, '--step-key', request.stepKey, '--status', status,
    ...argv, '--evidence-json', JSON.stringify(evidence),
  );

  let attempt;
  if (request.mode === 'send') {
    const checkedAt = Date.parse(intent.source.checked_at);
    const now = Date.now();
    requireThat(intent.step.status === 'reserved' && !intent.step.was_submitted && !task.halted &&
      !existsSync(attemptFile) && !existsSync(requestFile) && !existsSync(request.receiptFile) && !existsSync(conflictFile) &&
      Number.isFinite(checkedAt) && now - checkedAt <= 300000 && checkedAt <= now + 1000,
    'BRIDGE_SEND_REFUSED', 'An unused Bridge reservation and a current owned-source preflight are required.');
  } else {
    requireThat(request.localReceiptOnly !== true || existsSync(request.receiptFile),
      'BRIDGE_RECEIPT_MISSING', 'No durable receipt is available for this local-only read.');
    requireThat(['submitting', 'submitted', 'unknown', 'success'].includes(intent.step.status) &&
      sameContext(intent.step.evidence?.browser_context, intent.context),
    'BRIDGE_NO_ATTEMPT', 'The ledger has no Bridge submission with this browser binding.');
    if (!existsSync(attemptFile) && intent.step.status === 'submitting') {
      commit('unknown', {
        browser_context: intent.context, input_attempted: false, reason: 'The original press marker was never written.',
      });
      return output('unknown', intent.context, { receipt_status: 'no_local_press' });
    }
    requireThat(existsSync(attemptFile), 'BRIDGE_NO_ATTEMPT', 'The local press marker for this step is missing.');
    const marked = readRecord(attemptFile);
    requireThat(matchingAttempt(marked, intent.identity) && intent.step.evidence?.input_attempted !== false &&
      intent.step.evidence?.capture_conflicted !== true && !existsSync(conflictFile),
    'BRIDGE_CAPTURE_REJECTED', 'This attempt has no admissible export capture to recover.');
    attempt = { ...intent.identity, capture_not_before_ms: marked.capture_not_before_ms };
    if (existsSync(request.receiptFile)) {
      const receipt = recoverReceipt(request.receiptFile, attempt, intent.step.operator_id);
      rememberRequest(requestFile, attempt, {
        endpoint: receipt.endpoint, request_id: receipt.request_id, timestamp: receipt.timestamp,
      });
      return saveReceipt(receipt, true);
    }
    requireThat(intent.step.status === 'submitting' || intent.step.status === 'unknown',
      'BRIDGE_RECEIPT_MISSING', 'The existing completed capture must be recovered from its durable receipt.');
  }

  const { Page, sendCommand } = await (dependencies.loadOpenCLI ?? loadOpenCLI)(request.opencliRoot);
  const page = new Page(request.session, undefined, request.profile, 'foreground', 'browser', 'persistent');
  let bound;
  try {
    bound = await bindExistingBridgePage(page, request.projectId, { pageId: request.pageId });
  } catch {
    throw new BridgeCommandError('BRIDGE_PAGE_MISMATCH', 'The specified existing Studio tab could not be bound.');
  }
  requireThat(sameContext(bound, intent.context), 'BRIDGE_PAGE_MISMATCH', 'The browser did not bind the specified Studio tab.');

  if (request.mode === 'send') {
    const ui = new BridgeUI(page, request.projectId);
    try {
      enforceSource(await ui.snapshot(), intent);
      await ui.prepare();
    } catch (error) {
      if (error instanceof BridgeCommandError) throw error;
      throw new BridgeCommandError('BRIDGE_PREPARE_FAILED', 'The bound Studio tab did not present the verified Blender export controls.');
    }
    try {
      await ui.sendOnce({
        ledger: request.ledger, taskId: request.taskId, stepKey: request.stepKey, runLedger,
        verifySource: snapshot => enforceSource(snapshot, intent),
        beforeClaim: () => (dependencies.checkReceiver ?? checkReceiver)(request, intent.step.metadata.bridge_receiver),
        beforePress: () => {
          writeOnce(attemptFile, { ...intent.identity, capture_not_before_ms: Date.now() });
          const latest = runLedger(request.ledger, 'status', ...ledgerArgs);
          const lastIntent = readIntent(request, latest);
          requireThat(!latest.halted && lastIntent.step.status === 'submitting' && !lastIntent.step.was_submitted &&
            lastIntent.source.operator_id === intent.source.operator_id &&
            lastIntent.step.evidence?.capture_required === true &&
            sameContext(lastIntent.step.evidence?.browser_context, intent.context),
          'BRIDGE_CLAIM_LOST', 'The original Bridge submission is no longer claimed by this attempt.');
        },
        capturePattern: EXPORT_ENDPOINT,
      });
    } catch (error) {
      if (error instanceof BridgeCommandError) throw error;
      throw new BridgeCommandError('BRIDGE_SEND_INTERRUPTED', 'The one-shot send was interrupted. Inspect the original ledger step and read back its capture.');
    }
    return output('submitting', intent.context, { clicked_once: true });
  }

  let raw;
  try {
    raw = await sendCommand('network-capture-read', {
      session: intent.context.session, contextId: intent.context.profile, page: intent.context.page,
      surface: 'browser', windowMode: 'foreground', siteSession: 'persistent', retainIncomplete: true,
    });
  } catch {
    throw new BridgeCommandError('BRIDGE_CAPTURE_UNAVAILABLE', 'The same-page OpenCLI capture could not be read.');
  }
  let capture;
  try {
    capture = classifyAttemptCapture(raw, attempt);
    rememberRequest(requestFile, attempt, capture.request);
  } catch (error) {
    if (error.code === 'BRIDGE_CAPTURE_REJECTED') {
      const conflict = { ...attempt, capture_conflicted: true, reason: error.message };
      writeOnce(conflictFile, conflict);
      if (intent.step.status === 'submitting') commit('unknown', conflict);
    }
    throw error;
  }
  if (capture.receipt) return saveReceipt(capture.receipt, false);
  let status = intent.step.status;
  if (status === 'submitting' && Date.now() > attempt.capture_not_before_ms + CAPTURE_WINDOW_MS) {
    commit('unknown', { ...attempt, reason: 'No complete export receipt within the original capture window.' });
    status = 'unknown';
  }
  return output(status, intent.context, { receipt_status: 'pending', post_requests: capture.post_requests });

  function saveReceipt(receipt, alreadySaved) {
    const evidence = { ...attempt, export_receipt: receipt };
    if (!alreadySaved) writeOnce(request.receiptFile, evidence);
    let status = intent.step.status;
    if (status === 'submitting') {
      commit('submitted', evidence, '--operator-id', receipt.export_operator_id, '--project-id', receipt.project_id);
      status = 'submitted';
    }
    return output(status, intent.context, { receipt_status: 'observed', export_receipt: receipt });
  }
}

async function main() {
  try {
    const request = parseJson(readFileSync(0, 'utf8'));
    const result = await runBridge(request);
    console.log(JSON.stringify(result));
  } catch (error) {
    const known = error instanceof BridgeCommandError;
    console.log(JSON.stringify({
      ok: false,
      code: known ? error.code : 'BRIDGE_LOCAL_ERROR',
      error: known ? error.message : 'Bridge local execution failed. Inspect the original ledger and evidence files.',
    }));
    process.exitCode = 2;
  }
}

if (process.argv[1] && pathToFileURL(process.argv[1]).href === import.meta.url) await main();
