import { cli, Strategy } from '@jackwener/opencli/registry';
import {
  ArgumentError,
  CommandExecutionError,
  TimeoutError,
} from '@jackwener/opencli/errors';
import {
  callStudioRest,
  ensureTripoPage,
  requireBrowserPage,
  resolveRegion,
} from './_shared.mjs';

const ALLOWED_FORMATS = ['gltf', 'fbx', 'obj', 'stl', 'usdz', '3mf'];
const ALLOWED_FBX_PRESETS = ['blender', '3dsmax', 'mixamo'];
const ALLOWED_PACKAGING = ['zip', 'embedded'];
const ALLOWED_TEX_SIZES = [512, 1024, 2048, 4096, 8192];
const TERMINAL_FAILURE_STATUSES = new Set(['failed', 'expired', 'cancelled', 'banned', 'unknown']);

function parsePositiveInt(raw, fieldName, fallback) {
  const val = raw == null ? fallback : Number(raw);
  if (!Number.isInteger(val) || val <= 0) {
    throw new ArgumentError(`${fieldName} must be a positive integer`);
  }
  return val;
}

cli({
  site: 'tripo-studio',
  name: 'export-url',
  description: '安全发起 Tripo Studio 服务端导出并轮询获取签名下载链接（默认拦截非自有扣费资产）',
  access: 'write',
  domain: 'studio.tripo3d.com',
  strategy: Strategy.COOKIE,
  browser: true,
  navigateBefore: false,
  example: 'opencli --profile YOUR_PROFILE tripo-studio export-url <project_id> --model-format fbx --fbx-preset blender --confirm-export -f json',
  args: [
    {
      name: 'project-id',
      type: 'string',
      required: true,
      positional: true,
      help: 'Project UUID to export',
    },
    {
      name: 'model-format',
      type: 'string',
      default: 'gltf',
      choices: ALLOWED_FORMATS,
      help: '3D export format: gltf (.glb), fbx, obj, stl, usdz, 3mf',
    },
    {
      name: 'fbx-preset',
      type: 'string',
      default: 'blender',
      choices: ALLOWED_FBX_PRESETS,
      help: 'FBX axis/rig preset: blender, 3dsmax, mixamo',
    },
    {
      name: 'texture-packaging',
      type: 'string',
      default: 'embedded',
      choices: ALLOWED_PACKAGING,
      help: 'Texture packaging mode: embedded or zip',
    },
    {
      name: 'texture-size',
      type: 'int',
      default: 2048,
      help: 'Texture resolution: 512, 1024, 2048, 4096, 8192',
    },
    {
      name: 'name',
      type: 'string',
      help: 'Output file stem without extension',
    },
    {
      name: 'with-animation',
      type: 'boolean',
      default: false,
      help: 'Include skeleton/retargeted animations',
    },
    {
      name: 'animation-names', type: 'string',
      help: 'Comma-separated completed retarget names from this project; required with --with-animation',
    },
    {
      name: 'enable-bake-animation',
      type: 'boolean',
      default: false,
      help: 'Legacy current-pose STATIC baking; rejected for new exports until pose/frame mapping is verified',
    },
    {
      name: 'pivot-to-center-bottom',
      type: 'boolean',
      default: false,
      help: 'Move model pivot to bounding-box bottom center',
    },
    {
      name: 'pack-uv',
      type: 'boolean',
      default: false,
      help: 'Repack UV islands on export',
    },
    {
      name: 'confirm-export',
      type: 'boolean',
      default: false,
      help: 'Explicit confirmation flag required before invoking write export API',
    },
    {
      name: 'allow-nonowner-spend',
      type: 'boolean',
      default: false,
      help: 'Explicitly allow 5-credit spend if project is not owned by the active account',
    },
    {
      name: 'submit-only', type: 'boolean', default: false,
      help: 'Return the new export operator immediately so the caller can persist its receipt',
    },
    {
      name: 'resume-operator-id', type: 'string',
      help: 'Read an already recorded export operator and resolve download; never creates another export',
    },
    {
      name: 'poll-timeout-sec',
      type: 'int',
      default: 180,
      help: 'Maximum seconds to wait for export completion',
    },
    {
      name: 'region',
      type: 'string',
      default: 'cn',
      choices: ['cn', 'global'],
      help: 'Target Tripo Studio region (cn|global)',
    },
  ],
  columns: [
    'project_id',
    'operator_id',
    'format',
    'fbx_preset',
    'is_owner',
    'estimated_credits',
    'file_name',
    'model_url',
  ],
  func: async (page, kwargs) => {
    requireBrowserPage(page, 'tripo-studio/export-url');
    const regionCfg = resolveRegion(kwargs.region);
    const projectId = String(kwargs['project-id'] || kwargs.projectId || '').trim();
    if (!projectId) {
      throw new ArgumentError('project-id is required');
    }
    const confirmExport = Boolean(kwargs['confirm-export'] || kwargs.confirmExport);
    const resumeId = String(kwargs['resume-operator-id'] || kwargs.resumeOperatorId || '').trim();
    const submitOnly = Boolean(kwargs['submit-only'] || kwargs.submitOnly);
    if (resumeId && (!/^[a-zA-Z0-9-]{8,64}$/.test(resumeId) || submitOnly)) {
      throw new ArgumentError('Invalid resume operator or incompatible --submit-only');
    }
    if (!confirmExport && !resumeId) {
      throw new ArgumentError(
        'Refusing write operation without explicit --confirm-export flag.',
        'Pass --confirm-export after checking balance/quota and ledger lock.'
      );
    }

    const format = String(kwargs['model-format'] || kwargs.modelFormat || 'gltf').toLowerCase();
    if (!ALLOWED_FORMATS.includes(format)) {
      throw new ArgumentError(`Unsupported model-format "${format}"`);
    }
    const fbxPreset = String(kwargs['fbx-preset'] || kwargs.fbxPreset || 'blender').toLowerCase();
    if (!ALLOWED_FBX_PRESETS.includes(fbxPreset)) {
      throw new ArgumentError(`Unsupported fbx-preset "${fbxPreset}"`);
    }
    const texturePackaging = String(kwargs['texture-packaging'] || kwargs.texturePackaging || 'embedded').toLowerCase();
    if (!ALLOWED_PACKAGING.includes(texturePackaging)) {
      throw new ArgumentError(`Unsupported texture-packaging "${texturePackaging}"`);
    }
    const textureSize = parsePositiveInt(kwargs['texture-size'] || kwargs.textureSize, 'texture-size', 2048);
    if (!ALLOWED_TEX_SIZES.includes(textureSize)) {
      throw new ArgumentError(`texture-size must be one of: ${ALLOWED_TEX_SIZES.join(', ')}`);
    }
    const pollTimeoutSec = parsePositiveInt(kwargs['poll-timeout-sec'] || kwargs.pollTimeoutSec, 'poll-timeout-sec', 180);

    await ensureTripoPage(page, regionCfg);

    // Mandatory pre-export ownership & credit verification (never blind write)
    const detail = await callStudioRest(
      page,
      regionCfg,
      `/v2/studio/project/detail/v3/${encodeURIComponent(projectId)}`
    );
    const isOwner = Boolean(detail?.is_owner);
    const allowNonowner = Boolean(kwargs['allow-nonowner-spend'] || kwargs.allowNonownerSpend);
    if (!isOwner && !allowNonowner) {
      throw new ArgumentError(
        `Project ${projectId} is not owned by the active account and exporting would cost 5 credits.`,
        'Re-run with --allow-nonowner-spend only if budgeted and approved.'
      );
    }

    const stem = String(kwargs.name || detail?.name || projectId)
      .replace(/[\\/:*?"<>|]/g, '_')
      .trim() || projectId;

    const animationNames = String(kwargs['animation-names'] || kwargs.animationNames || '').split(',').map(s => s.trim()).filter(Boolean);
    const withAnimation = Boolean(kwargs['with-animation'] || kwargs.withAnimation);
    const bakeAnimation = Boolean(kwargs['enable-bake-animation'] || kwargs.enableBakeAnimation);
    const available = new Set((detail?.operator?.retarget || []).filter(a => a?.name && a?.model_url).map(a => a.name));
    if (!resumeId && bakeAnimation) throw new ArgumentError('Current-pose static baking is not Action baking; its pose/frame mapping is unverified. Omit --enable-bake-animation for an animation export.');
    if (!resumeId && animationNames.length && !withAnimation) throw new ArgumentError('Animation selection requires --with-animation.');
    if (!resumeId && withAnimation && (!animationNames.length || animationNames.some(n => !available.has(n)) || new Set(animationNames).size !== animationNames.length)) {
      throw new ArgumentError('Select explicit completed retarget names from this project before animation export.');
    }
    // The CLI selects by readable name; Studio's export API consumes operator
    // IDs. Its UI maps checked retarget rows to row.operator_id, not row.name.
    // Resolve only when creating an export. Historical operator recovery does
    // not submit a body and must remain independent of current project rows.
    const animationOperatorIds = !resumeId && withAnimation ? animationNames.map(name => {
      const candidates = (detail?.operator?.retarget || []).filter(a => a?.name === name && a?.model_url);
      if (candidates.length !== 1 || typeof candidates[0].operator_id !== 'string' || !/^[A-Za-z0-9-]{8,64}$/.test(candidates[0].operator_id)) {
        throw new ArgumentError('Selected retarget name has a missing or ambiguous operator ID; no export submitted.');
      }
      return candidates[0].operator_id;
    }) : [];
    if (new Set(animationOperatorIds).size !== animationOperatorIds.length) throw new ArgumentError('Distinct retarget names resolved to the same operator ID; no export submitted.');
    const exportBody = {
      animate_in_place: false,
      animations: animationOperatorIds,
      bake_animation_frame: 0,
      enable_bake_animation: bakeAnimation,
      export_vertex_colors: false,
      fbx_preset: fbxPreset,
      format,
      model_version: 'default',
      name: stem,
      pack_uv: Boolean(kwargs['pack-uv'] || kwargs.packUv),
      pivot_to_center_bottom: Boolean(kwargs['pivot-to-center-bottom'] || kwargs.pivotToCenterBottom),
      project_id: projectId,
      texture_packaging: texturePackaging,
      texture_size: textureSize,
      with_animation: withAnimation,
    };

    const exportEndpoint = isOwner
      ? '/v2/studio/operation/export'
      : '/v2/studio/operation/export-public-model';

    const exportRes = resumeId ? {} : await callStudioRest(page, regionCfg, exportEndpoint, {
      method: 'POST', body: exportBody,
    });
    const exportOpId = String(resumeId || exportRes?.operator_id || '');
    if (!exportOpId) {
      throw new CommandExecutionError('Export API returned neither operator_id nor model_url');
    }

    const ext = format === 'gltf' ? 'glb' : format;
    const expectedFile = texturePackaging === 'zip' && format !== 'gltf' ? `${stem}.zip` : `${stem}.${ext}`;
    if (submitOnly) {
      return [{project_id: projectId, operator_id: exportOpId, format, fbx_preset: fbxPreset,
        is_owner: isOwner, estimated_credits: isOwner ? 0 : 5, file_name: expectedFile,
        model_url: '', state: 'submitted'}];
    }

    let finalModelUrl = String(exportRes?.model_url || '');
    if (!finalModelUrl) {
      let completed = false;
      const deadline = Date.now() + pollTimeoutSec * 1000;
      while (Date.now() < deadline) {
        const prog = await callStudioRest(page, regionCfg, '/v2/studio/progress', {
          method: 'POST',
          body: { ids: [exportOpId] },
        });
        const items = Array.isArray(prog)
          ? prog
          : Array.isArray(prog?.tasks)
          ? prog.tasks
          : Object.values(prog || {});
        const entry = items.find((it) => it && (it.operator_id === exportOpId || it.id === exportOpId));
        if (!entry) throw new CommandExecutionError('Progress did not match the recorded export operator; no retry');
        const status = String(entry?.status || '').toLowerCase();
        if (status === 'success') {
          completed = true;
          finalModelUrl = String(entry?.output?.model || entry?.model_url || '');
          break;
        }
        if (TERMINAL_FAILURE_STATUSES.has(status)) {
          throw new CommandExecutionError(
            `Export operator ${exportOpId} terminated with status=${status} (NO auto-retry).`
          );
        }
        await new Promise((resolve) => setTimeout(resolve, 2000));
      }
      if (!completed) {
        throw new TimeoutError('tripo-studio export-url', pollTimeoutSec, 'Check progress manually before retrying.');
      }
    }

    const dl = await callStudioRest(page, regionCfg, '/v2/studio/operation/download_with_name', {
      method: 'POST',
      body: { file_name: stem, operator_id: exportOpId },
    });
    const signedUrl = String(dl?.model_url || finalModelUrl);
    if (!signedUrl) {
      throw new CommandExecutionError(`download_with_name did not return model_url for ${exportOpId}`);
    }

    return [
      {
        project_id: projectId,
        operator_id: exportOpId,
        format,
        fbx_preset: fbxPreset,
        is_owner: isOwner,
        estimated_credits: isOwner ? 0 : 5,
        file_name: expectedFile,
        model_url: signedUrl,
      },
    ];
  },
});
