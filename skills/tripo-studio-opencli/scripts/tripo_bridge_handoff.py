"""Hand a specific completed Studio Bridge transfer to local Blender modeling."""
import hashlib
import json
import os
from pathlib import Path
import tempfile

from tripo_bridge import _call_page, _name, _plan, _receipt_file, _uuid
from tripo_bridge_candidate import BLENDER_EXECUTABLE_SHA256, BLENDER_HOST, build_candidate
from tripo_ledger import _ledger_lock, _save_ledger_atomic, cmd_status


def add_parser(sub):
    p = sub.add_parser("bridge-handoff", help="Save the received scene and deliver a cold-verified modeling candidate")
    p.add_argument("--task-id", required=True, help="Original Studio budget task")
    p.add_argument("--step-key", required=True, help="Original Bridge export step")
    p.add_argument("--project-id", required=True, help="Original Studio project UUID")
    p.add_argument("--output-dir", required=True, help="New, absolute delivery directory; reuse the same directory to resume")
    p.add_argument("--source-operator-id", help="Optionally require this exact Studio variation")
    p.add_argument("--blender", help=f"Windows Blender {BLENDER_HOST['version']} build {BLENDER_HOST['build']} executable matching the project runtime")


def _read(path):
    with Path(path).open("r", encoding="utf-8-sig") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise ValueError("Expected a JSON object in the Bridge records.")
    return data


def _sha(path):
    with Path(path).open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _saved_file(record):
    file = Path(record["saved_copy"])
    _require(file.is_absolute() and file.suffix.lower() == ".blend" and file.is_file(),
             "The receiver's saved working copy is unavailable.")
    _require(_sha(file) == record["saved_sha256"], "The saved receiver file differs from its recorded bytes.")
    return file


def _received(record, project, export):
    transfer = record.get("receiver") or {}
    _require(record.get("ok") is True and record.get("export_uuid") == export
             and record.get("pending_imports") == 0 and transfer.get("file_id") == project
             and transfer.get("phase") == "complete" and transfer.get("percent") == 100 and not transfer.get("error"),
             "The receiver record does not match a completed transfer for this project and export.")
    _require(isinstance(record.get("target_scene"), str) and record["target_scene"]
             and isinstance(record.get("added_objects"), dict) and record["added_objects"],
             "The receiving scene or imported object list is missing.")
    return record


def _archived_source(args, step):
    # The original ledger identifies all three records. A selected .blend or
    # an old authorization file on its own does not identify a transfer.
    ev = step["evidence"]
    receiver_path, capture_path, cold_path = (Path(ev[k]) for k in ("receiver", "export_receipt", "cold_components"))
    folder = receiver_path.resolve(strict=True).parent
    _require(all(p.is_absolute() and p.resolve(strict=True).parent == folder for p in (capture_path, cold_path)),
             "The original step does not point to a single archived transfer.")
    capture, cold = _read(capture_path), _read(cold_path)
    export = _uuid(step["operator_id"], "export operator")
    received = _received(_read(receiver_path), args.project_id, export)
    posts = [r for r in capture["operations"] if r.get("method") != "OPTIONS" and
             r.get("endpoint") in {"/v2/studio/operation/export", "/v2/studio/operation/export-public-model"}]
    _require(len(posts) == 1, "The archive does not contain one identifiable export.")
    row = posts[0]
    required = {"project_id": args.project_id, "format": "fbx", "fbx_preset": "blender",
                "texture_packaging": "embedded", "texture_size": 8192, "with_animation": False, "enable_bake_animation": False}
    _require(step["project_id"] == args.project_id and capture["browser_context"]["project_id"] == args.project_id
             and row["method"] == "POST" and row["endpoint"] == "/v2/studio/operation/export"
             and row["http"] == 200 and row["capture_complete"] is True and row.get("request_id")
             and row["response"]["code"] == 0 and row["response"]["operator_id"] == export
             and all(type(row["request"].get(k)) is type(v) and row["request"][k] == v for k,v in required.items()),
             "Archived export parameters or identity differ from this step.")
    _require(cold.get("export_uuid") == export and cold.get("cold_8k_basecolor_packed_and_linked") is True
             and cold.get("sha256") == received["saved_sha256"] and
             Path(cold["candidate"]).resolve() == Path(received["saved_copy"]).resolve(),
             "The independent archived cold read differs from the receiver record.")
    original_source = _read(folder / "SOURCE-PREFLIGHT.json")
    original_check = step["metadata"]["bridge_source"]
    _require(original_source.get("project_id") == original_check.get("project_id") == args.project_id
             and original_source.get("checked_at") and original_source["checked_at"] == original_check.get("checked_at")
             and original_source.get("is_owner") is True and original_check.get("owner_verified") is True
             and original_source.get("status") == "success",
             "The archived variation preflight is not the one recorded by this step.")
    source = _uuid(original_source.get("operator_id"), "archived source operator")
    return source, export, received, {"original_world_matrix_check": cold.get("strict_original_matrix_equality")}


def _live_source(driver, args, step):
    meta = step["metadata"]
    source = _uuid(meta["bridge_source"]["operator_id"], "source operator")
    receipt = _receipt_file(args)
    _require(receipt.is_file(), "Recover the original export receipt with bridge --read-receipt first.")
    result = _call_page(driver, args, "receipt", meta["bridge_context"], receipt, local_receipt_only=True)
    export_receipt = result.get("export_receipt") or {}
    export = _uuid(export_receipt.get("export_operator_id"), "export operator")
    _require(export_receipt.get("source_operator_id") == source and export_receipt.get("project_id") == args.project_id
             and (not step.get("operator_id") or step["operator_id"] == export), "Different Studio transfer in the original receipt.")
    _require(meta.get("bridge_receiver_baseline", {}).get("state") == "receiver_ready",
             "The original submission did not retain the receiver baseline required to identify this import.")
    return source, export, None, {}


def _capture(step, identity, output):
    from tripo_bridge_receiver import save_received
    cache = output.with_name(output.name + ".receiver")
    owner = cache / "capture.json"
    if cache.exists():
        _require(owner.is_file() and not cache.is_symlink(), "The receiver cache is not owned by this handoff.")
        state = _read(owner)
        _require(state["identity"] == identity, "An unrelated capture already occupies this directory.")
        stage = Path(state["directory"])
        _require(stage.resolve().parent == cache.resolve() and not stage.is_symlink(), "Invalid capture directory.")
        if (stage / "receiver.json").is_file():
            received = _received(_read(stage / "receiver.json"), identity["project_id"], identity["export_operator_id"])
            baseline = step["metadata"]["bridge_receiver_baseline"]
            _require(received.get("state") == "receiver_saved" and received.get("blender_pid") == baseline["pid"]
                     and received["target_scene"] == baseline["scene"]
                     and received.get("snapshot_file_sha256") == baseline["snapshot_file_sha256"]
                     and received["receiver"].get("revision") == baseline["transfer_revision"] + 1,
                     "The saved receiver snapshot differs from the baseline for this step.")
            _require(Path(received["saved_copy"]).resolve() == stage.resolve() / "received.blend",
                     "The saved receiver path does not belong to this capture.")
            return received
    else:
        with tempfile.TemporaryDirectory(prefix=".bridge-init-", dir=output.parent) as temp:
            initial = Path(temp) / "cache"
            initial.mkdir()
            _save_ledger_atomic(initial / owner.name, {"identity": identity, "directory": str(cache / "first")})
            os.rename(initial, cache)
    stage = Path(tempfile.mkdtemp(dir=cache, prefix="capture-"))
    _save_ledger_atomic(owner, {"identity": identity, "directory": str(stage)})
    meta = step["metadata"]
    received = save_received(meta["bridge_receiver"], meta["bridge_receiver_baseline"], identity["project_id"],
                             identity["export_operator_id"], stage)
    return _received(received, identity["project_id"], identity["export_operator_id"])


def _check_delivery(output, identity, pinned):
    manifest = output / "handoff.json"
    _require(manifest.is_file() and not manifest.is_symlink(), "The destination already contains unrelated or incomplete work.")
    data = _read(manifest)
    _require(data.get("identity") == identity and data.get("candidate") == "candidate.blend",
             "The existing handoff belongs to a different Studio task, step or file.")
    candidate = output / "candidate.blend"
    _require(candidate.is_file() and not candidate.is_symlink() and _sha(candidate) == data["sha256"],
             "The existing candidate has changed. Preserve edits and inspect its original handoff.")
    _require(pinned and pinned["manifest_sha256"] == _sha(manifest) and pinned["candidate_sha256"] == data["sha256"],
             "This handoff differs from the verified publication record.")
    verification = data.get("verification") or {}
    _require(verification.get("blender_host") == BLENDER_HOST and verification.get("blender_build") == BLENDER_HOST["build"]
             and verification.get("cold_reopened") is True,
             f"Existing handoff lacks Windows Blender {BLENDER_HOST['version']} build {BLENDER_HOST['build']} cold verification. "
             "Keep the historical delivery and extract from the archived receiver into a new directory.")
    return data


def _run_locked(driver, args, output, blender):
    task = cmd_status(args)
    step = task["steps"].get(args.step_key) or {}
    meta = step.get("metadata") or {}
    _require(task["region"] == "cn" and step.get("operation") == "export" and step.get("credits") == 0
             and step.get("downloads") == 1 and meta.get("bridge_plan") == _plan(args.project_id)
             and step.get("status") in {"submitted", "unknown", "success"}, "No matching submitted Bridge step in this ledger.")
    ev = step.get("evidence") or {}
    pinned = ev.get("bridge_handoff")
    archived = step["status"] == "success" and isinstance(ev.get("receiver"), str) and "cold_components" in ev
    if archived:
        source, export, received, qualifications = _archived_source(args, step)
    elif pinned and step["status"] == "success":
        _require(step.get("project_id") == args.project_id, "The committed handoff belongs to a different project.")
        source = _uuid(meta["bridge_source"]["operator_id"], "committed source operator")
        export = _uuid(step["operator_id"], "committed export operator")
        received, qualifications = None, {}
    else:
        source, export, received, qualifications = _live_source(driver, args, step)
    _require(args.source_operator_id in (None, source), "The selected Studio variation differs from this step.")
    identity = {"task_id": args.task_id, "step_key": args.step_key, "project_id": args.project_id,
                "source_operator_id": source, "export_operator_id": export}
    journal = (output.with_name(output.name + ".bridge-proof.json") if archived
               else _receipt_file(args).with_suffix(".handoff-ready.json"))
    prepared = None
    if journal.exists():
        saved = _read(journal)
        _require(saved.get("identity") == identity, "The publication record belongs to a different transfer.")
        prepared = saved["handoff"]
    for proof in (pinned, prepared):
        if proof:
            _require(output / "handoff.json" == Path(proof["manifest"]).resolve(),
                     "Use the delivery directory already recorded for this step.")
    reused = output.exists()
    if reused:
        data = _check_delivery(output, identity, pinned or prepared)
    else:
        _require(not pinned, "The recorded delivery is missing. Inspect its recovery copies.")
        if received is None:
            received = _capture(step, identity, output)
        received_blend = _saved_file(received)
        # Publish the directory only after both the native file and the manifest
        # are durable. The existing live receipt stays available if this fails.
        with tempfile.TemporaryDirectory(prefix=".bridge-handoff-", dir=output.parent) as temp:
            stage = Path(temp) / "delivery"
            stage.mkdir()
            candidate = build_candidate(blender, received_blend, received["target_scene"], list(received["added_objects"]),
                                        received["saved_sha256"], stage)
            _require(candidate["ok"] is True and Path(candidate["candidate"]).resolve() == stage / "candidate.blend"
                     and _sha(stage / "candidate.blend") == candidate["sha256"], "Candidate did not pass independent verification.")
            data = {"schema_version": 1, "identity": identity, "recipe": meta["bridge_plan"],
                    "candidate": "candidate.blend", "sha256": candidate["sha256"], "received_file": str(received_blend),
                    "received_sha256": received["saved_sha256"], "scene": candidate["scene"], "objects": candidate["objects"],
                    "verification": candidate["verification"], "source": "archived_receiver" if archived else "bound_receiver",
                    "original_qualifications": qualifications}
            _save_ledger_atomic(stage / "handoff.json", data)
            # Bind the delivery before exposing it; a caller-provided or edited
            # manifest in output must never be able to settle a ledger step.
            ready = {"manifest": str(output / "handoff.json"), "manifest_sha256": _sha(stage / "handoff.json"),
                     "candidate_sha256": data["sha256"]}
            _save_ledger_atomic(journal, {"identity": identity, "handoff": ready})
            os.rename(stage, output)
    manifest = output / "handoff.json"
    proof = {"manifest": str(manifest), "manifest_sha256": _sha(manifest), "candidate_sha256": data["sha256"]}
    current = cmd_status(args)["steps"][args.step_key]
    _require(current.get("metadata") == meta and current.get("operator_id") in (None, "", export)
             and current.get("status") in {"submitted", "success", "unknown"},
             "The ledger step changed while Blender was verifying the candidate.")
    if current["status"] == "submitted":
        driver._ledger(Path(args.ledger), "commit", "--task-id", args.task_id, "--step-key", args.step_key,
                       "--status", "success", "--project-id", args.project_id, "--operator-id", export,
                       "--actual-credits", "0", "--evidence-json", json.dumps({**(current.get("evidence") or {}), "bridge_handoff": proof}))
    final = cmd_status(args)
    return {"ok": True, "status": final["steps"][args.step_key]["status"], "halted": final["halted"], "receiver_verified": True,
            "reused": reused, "candidate": str(output / "candidate.blend"), "handoff": proof, "identity": identity,
            "blender_host": data["verification"]["blender_host"]}


def run(driver, args):
    _require(args.region == "cn", "This recipe requires CN Studio.")
    _uuid(args.project_id, "--project-id")
    _name(args.task_id, "--task-id")
    _name(args.step_key, "--step-key")
    given = Path(args.output_dir)
    _require(given.is_absolute() and not given.is_symlink(), "Choose an absolute delivery directory.")
    output = given.resolve()
    _require(output.parent != output and output.parent.is_dir(), "Create the delivery parent directory first.")
    version = BLENDER_HOST["version"]
    blender = (Path(args.blender) if args.blender else driver.DEFAULT_RUNTIME_DIR.parent / "blender" / "releases"
               / version / f"blender-{version}-windows-x64" / "blender.exe")
    blender = blender.resolve()
    _require(blender.is_file() and _sha(blender) == BLENDER_EXECUTABLE_SHA256,
             f"Blender executable rejected before launch. Use the project Windows Blender {version} "
             f"build {BLENDER_HOST['build']} executable or a byte-identical copy.")
    # Consistent order across processes, including simultaneous calls naming different outputs.
    task_lock = _receipt_file(args).with_suffix(".handoff")
    original = cmd_status(args)["steps"].get(args.step_key) or {}
    historical = original.get("status") == "success" and "cold_components" in (original.get("evidence") or {})
    with _ledger_lock(output.with_name(output.name + ".bridge-publish")):
        if historical:
            return _run_locked(driver, args, output, blender)
        with _ledger_lock(task_lock):
            return _run_locked(driver, args, output, blender)
