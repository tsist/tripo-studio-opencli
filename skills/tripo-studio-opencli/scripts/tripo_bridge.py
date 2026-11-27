"""Plan, send once, or recover an official Studio → Blender Bridge transfer."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from types import SimpleNamespace
from uuid import UUID

from tripo_bridge_receiver import RECEIVER_BUILD, RECEIVER_HOST
from tripo_ledger import _ledger_lock, _save_ledger_atomic, cmd_status
from tripo_pricing import estimate_postprocess


def add_parser(sub):
    p = sub.add_parser("bridge", help="Official Studio → Blender Bridge; default is an offline plan")
    p.add_argument("--task-id", required=True, help="Original budget ledger task")
    p.add_argument("--step-key", required=True, help="One transfer attempt in that task")
    p.add_argument("--project-id", required=True, help="CN Studio project UUID")
    p.add_argument("--source-operator-id", help="Approved source variation/operator UUID")
    p.add_argument("--session", help="Existing OpenCLI browser session")
    p.add_argument("--page-id", help="Existing Studio model tab identity")
    p.add_argument("--receiver-pid", type=int, help=f"PID of the interactive {RECEIVER_HOST} build {RECEIVER_BUILD}")
    p.add_argument("--mcp-port", type=int, help="Blender Lab port of the same process")
    p.add_argument("--work-file", help="Absolute path of the saved empty receiver .blend")
    p.add_argument("--scene", help="Active scene in that working copy")
    p.add_argument("--checkpoint", help="Independent byte-identical backup of the saved working copy")
    modes = p.add_mutually_exclusive_group()
    modes.add_argument("--status", action="store_true", help="Read this step locally")
    modes.add_argument("--execute-spend", action="store_true",
                       help="Send once for the approved project, source, page and receiver (one download)")
    modes.add_argument("--read-receipt", action="store_true",
                       help="Recover only the original export receipt; never send again")


def _uuid(value, field):
    try:
        if str(UUID(value)) == value:
            return value
    except (ValueError, AttributeError, TypeError):
        pass
    raise ValueError(f"{field} must be a canonical lowercase UUID.")


def _name(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value):
        raise ValueError(f"Missing or invalid {label}.")
    return value


def _context(args):
    return {"profile": args.profile, "session": args.session,
            "page": args.page_id, "project_id": args.project_id}


def _receiver(args):
    return {"pid": args.receiver_pid, "mcp_port": args.mcp_port, "work_file": args.work_file,
            "scene": args.scene, "checkpoint": args.checkpoint}


def _plan(project):
    return {"project_id": project, "model_format": "fbx", "fbx_preset": "blender",
            "texture_size": 8192, "texture_packaging": "embedded"}


def _checked_step(args, task):
    if task["region"] != "cn":
        raise ValueError("This task belongs to a different Studio region.")
    step = task["steps"].get(args.step_key)
    if step is None:
        return None
    meta = step.get("metadata") or {}
    if step.get("operation") != "export" or meta.get("bridge_plan") != _plan(args.project_id):
        raise ValueError("This step belongs to a different export/project/recipe.")
    for requested, stored in [(_context(args), meta.get("bridge_context") or {}),
                              (_receiver(args), meta.get("bridge_receiver") or {})]:
        for key, value in requested.items():
            if key != "project_id" and value is not None and value != stored.get(key):
                raise ValueError(f"Bridge step binding differs: {key}.")
    if args.source_operator_id and args.source_operator_id != (meta.get("bridge_source") or {}).get("operator_id"):
        raise ValueError("Bridge step belongs to a different source operator.")
    return step


def _status(args, task, step):
    handoff = ((step or {}).get("evidence") or {}).get("bridge_handoff")
    verified = bool(handoff and step["status"] == "success")
    baseline = (((step or {}).get("metadata") or {}).get("bridge_receiver_baseline") or {})
    recorded = {key: baseline[key] for key in ("receiver_host", "receiver_build") if key in baseline}
    return {"ok": True, "task_id": args.task_id, "step_key": args.step_key, "project_id": args.project_id,
            "status": step["status"] if step else "not_reserved", "halted": task["halted"],
            "export_operator_id": (step or {}).get("operator_id") or None,
            "downloads_remaining": task["resource_usage"]["downloads_remaining"],
            "receiver_requirement": {"host": RECEIVER_HOST, "build": RECEIVER_BUILD, "interactive": True},
            "receiver_verified": verified, **({"recorded_receiver": recorded} if recorded else {}),
            **({"handoff": handoff} if verified else {})}


def _source(driver, args):
    # An adapter error can include account data. Keep remote output in memory only.
    try:
        rows = driver._extract_json(driver._opencli(args.profile, "tripo-studio", "project", args.project_id,
                                                   "--operator-id", args.source_operator_id, "--region", "cn", "-f", "json"))
    except Exception:
        raise RuntimeError("Studio project preflight failed; no transfer was submitted.") from None
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise ValueError("Studio must return one source project.")
    row = rows[0]
    expected = {"project_id": args.project_id, "operator_id": args.source_operator_id,
                "status": "success", "is_owner": True, "is_rigged": False, "is_ultra_textured": True}
    if any(type(row.get(k)) is not type(v) or row[k] != v for k, v in expected.items()):
        raise ValueError("The Studio source, ownership, static model, or 8K texture no longer matches.")
    credits = estimate_postprocess("export", is_owner=True).net_credits
    if credits != 0:
        raise ValueError("Owned export estimate changed.")
    return {"project_id": args.project_id, "operator_id": args.source_operator_id, "owner_verified": True,
            "net_credits": credits, "checked_at": datetime.now(timezone.utc).isoformat()}


def _node_command(driver):
    # Follow the same package bin that the public driver resolved on Windows.
    if len(driver.OPENCLI_COMMAND) == 2:
        for parent in Path(driver.OPENCLI_COMMAND[1]).resolve().parents:
            if parent.name == "opencli" and parent.parent.name == "@jackwener" and (parent / "package.json").is_file():
                return driver.OPENCLI_COMMAND[0], parent
    raise RuntimeError("The installed @jackwener/opencli Node entry could not be resolved.")


def _call_page(driver, args, mode, context, receipt_file, receiver_check=None, *, local_receipt_only=False):
    scripts = Path(__file__).resolve().parent
    payload = {"mode": mode, "python": sys.executable,
               "ledgerScript": str(scripts / "tripo_ledger.py"), "ledger": str(Path(args.ledger).resolve()),
               "profile": context["profile"], "session": context["session"], "pageId": context["page"],
               "projectId": args.project_id, "taskId": args.task_id, "stepKey": args.step_key,
               "receiptFile": str(receipt_file), "localReceiptOnly": local_receipt_only}
    if local_receipt_only:
        binary = shutil.which("node")
        if not binary:
            raise RuntimeError("Reading the saved Bridge receipt requires the local Node runtime.")
    else:
        binary, package = _node_command(driver)
        payload["opencliRoot"] = str(package)
    if receiver_check is not None:
        payload["receiverCheck"] = receiver_check
    try:
        process = subprocess.run([binary, str(scripts / "tripo_bridge_command.mjs")],
                                 input=json.dumps(payload), capture_output=True, encoding="utf-8",
                                 timeout=120, check=False)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Bridge command timed out. Inspect the original step before any further action.") from None
    try:
        response = json.loads(process.stdout)
    except ValueError:
        response = {}
    if process.returncode or not isinstance(response, dict) or response.get("ok") is not True:
        # The Node entry sanitizes errors. Never relay raw browser or process output.
        detail = response.get("code") if isinstance(response, dict) else None
        code = detail if isinstance(detail, str) and re.fullmatch(r"[A-Z0-9_]{1,100}", detail) else "CHECK_STEP"
        raise RuntimeError(f"Bridge {mode} could not finish ({code}). Inspect the original step.")
    return response


def _receipt_file(args):
    ledger = Path(args.ledger).resolve()
    digest = hashlib.sha256(json.dumps([args.task_id, args.step_key]).encode("utf-8")).hexdigest()
    return ledger.with_name(ledger.name + ".bridge-receipts") / (digest + ".json")


def _page_record(driver, context):
    # OpenCLI sessions can point at the same physical tab. One owner across
    # all custom ledger/runtime directories may read its unfinished capture.
    key = json.dumps([context["profile"], context["page"]]).encode("utf-8")
    return Path(driver.DEFAULT_RUNTIME_DIR) / "bridge-tabs" / (hashlib.sha256(key).hexdigest() + ".json")


def _owner(args):
    return {"ledger": str(Path(args.ledger).resolve()), "task_id": args.task_id, "step_key": args.step_key}


def _read_owner(record):
    return json.loads(record.read_text(encoding="utf-8")) if record.exists() else None


def _claimable(record):
    old = _read_owner(record)
    if old is None:
        return
    previous = SimpleNamespace(**old)
    step = cmd_status(previous)["steps"][previous.step_key]
    receipt = _receipt_file(previous)
    if step["status"] == "aborted_before_submit" or (step["status"] in {"submitted", "success"} and receipt.is_file()):
        return
    marker = Path(str(receipt) + ".attempt")
    if not marker.exists() and step["status"] == "unknown" and (step.get("evidence") or {}).get("input_attempted") is False:
        return
    if marker.is_file():
        start = json.loads(marker.read_text(encoding="utf-8"))["capture_not_before_ms"]
        if time.time_ns() // 1_000_000 > start + 45_000:
            return
    raise ValueError("This Studio tab has an unresolved Bridge attempt. Read its original receipt first.")


def _read_capture(driver, args, context, receipt, page_record):
    result = _call_page(driver, args, "receipt", context, receipt)
    if result.get("receipt_status") == "observed" and receipt.is_file():
        # The page lock is held and the durable receipt now owns all recovery.
        page_record.unlink(missing_ok=True)
    return result


def _transfer(driver, args):
    task = cmd_status(args)
    step = _checked_step(args, task)
    if args.read_receipt:
        if step is None:
            raise ValueError("No such Bridge attempt exists in this ledger.")
        if step["status"] not in {"submitting", "unknown", "submitted", "success"}:
            return _status(args, task, step)
        context = (step.get("metadata") or {}).get("bridge_context")
        if not context or context.get("project_id") != args.project_id:
            raise ValueError("This historical step lacks a bound page. Use its original readback evidence.")
        receipt = _receipt_file(args)
        if receipt.is_file():
            return _call_page(driver, args, "receipt", context, receipt, local_receipt_only=True)
        page_record = _page_record(driver, context)
        with _ledger_lock(page_record):
            if _read_owner(page_record) != _owner(args):
                raise ValueError("This step no longer owns the Studio tab capture. Use its saved receipt.")
            return _read_capture(driver, args, context, receipt, page_record)
    if step is not None:
        return _status(args, task, step)
    if task["schema_version"] < 2 or task["halted"] or task["submitting"] or task["unresolved_unknown"]:
        raise ValueError("Bridge requires an active CN schema-v2 task with no unresolved submission.")
    remaining = task["resource_usage"]
    if remaining["submissions_remaining"] < 1 or remaining["downloads_remaining"] < 1:
        raise ValueError("This ledger has no remaining submission or download allowance.")
    context = _context(args)
    for key in ("profile", "session", "page"):
        _name(context[key], key)
    _uuid(args.source_operator_id, "--source-operator-id")
    receiver = _receiver(args)
    if not all(receiver.values()):
        raise ValueError(f"Specify the interactive {RECEIVER_HOST} receiver PID, MCP port, working .blend, scene and checkpoint.")
    page_record = _page_record(driver, context)
    with _ledger_lock(page_record):
        _claimable(page_record)
        return _send(driver, args, context, receiver, page_record)


def _send(driver, args, context, receiver, page_record):
    from tripo_bridge_receiver import inspect_receiver
    baseline = inspect_receiver(receiver)
    if baseline.get("ok") is not True:
        raise ValueError("Receiver check failed before reservation.")
    _node_command(driver)
    driver._ensure_daemon(args.profile)
    source = _source(driver, args)
    meta = {"studio_profile": args.profile, "bridge_plan": _plan(args.project_id), "bridge_context": context,
            "bridge_receiver": receiver, "bridge_receiver_baseline": baseline, "bridge_source": source}
    receipt = _receipt_file(args)
    receipt.parent.mkdir(parents=True, exist_ok=True)
    if receipt.exists() or Path(str(receipt) + ".attempt").exists():
        raise ValueError("An earlier attempt record exists. Inspect the original ledger.")
    driver._ledger(Path(args.ledger), "reserve", "--task-id", args.task_id, "--step-key", args.step_key,
                   "--operation", "export", "--credits", "0", "--downloads", "1",
                   "--metadata-json", json.dumps(meta))
    try:
        _save_ledger_atomic(page_record, _owner(args))
        _call_page(driver, args, "send", context, receipt, {"binding": receiver, "baseline": baseline})
    except Exception:
        # Pre-claim preparation cannot press Send. A crash after the claim must
        # preserve submitting/unknown for receipt recovery; never undo or replay.
        current = _checked_step(args, cmd_status(args))
        if current and current["status"] == "reserved":
            driver._ledger(Path(args.ledger), "commit", "--task-id", args.task_id, "--step-key", args.step_key,
                           "--status", "aborted_before_submit", "--evidence-json",
                           json.dumps({"reason": "Bridge command stopped before the atomic input claim"}))
            if _read_owner(page_record) == _owner(args):
                page_record.unlink(missing_ok=True)
        raise
    return _read_capture(driver, args, context, receipt, page_record)


def run(driver, args):
    if args.region != "cn":
        raise ValueError("This Bridge recipe is verified only for CN Studio.")
    _uuid(args.project_id, "--project-id")
    _name(args.task_id, "--task-id")
    _name(args.step_key, "--step-key")
    if args.source_operator_id is not None:
        _uuid(args.source_operator_id, "--source-operator-id")
    if args.status:
        task = cmd_status(args)
        return _status(args, task, _checked_step(args, task))
    if not args.execute_spend and not args.read_receipt:
        return {"ok": True, "status": "plan", "submitted": False, "project_id": args.project_id,
                "recipe": _plan(args.project_id), "receiver_host": RECEIVER_HOST, "receiver_build": RECEIVER_BUILD,
                "estimate_if_owned": {"credits": 0, "downloads": 1, "submissions": 1}}
    with _ledger_lock(Path(args.runtime_dir) / "studio-browser.lock"):
        return _transfer(driver, args)
