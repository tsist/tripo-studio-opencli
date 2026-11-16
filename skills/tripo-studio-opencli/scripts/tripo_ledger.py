#!/usr/bin/env python3
"""
Atomic task credit-cap and idempotency ledger for Tripo Studio Web automation.
Prevents duplicate credit spend and hard-stops on failure or unknown states.

Schema v2 adds cross-process locking, irreversible submission history,
submission/trial/download budgets (including zero-credit work), metadata and
evidence receipts, and actual-credit reconciliation. Legacy v1 tasks remain
readable and keep their reservations and unknown spend accounted.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from tripo_pricing import estimate_generate, estimate_postprocess

DEFAULT_RUNTIME_DIR = Path(os.environ.get("TRIPO_STUDIO_RUNTIME_DIR", str(Path.cwd() / ".tripo-studio")))
DEFAULT_LEDGER_PATH = DEFAULT_RUNTIME_DIR / "ledger.json"

SCHEMA_VERSION = 2
LOCK_TIMEOUT_SECONDS = 8.0
TERMINAL_HALT_STATUSES = {"failed", "expired", "cancelled", "banned"}
UNKNOWN_STATUS = "unknown"
SUBMITTING_STATUS = "submitting"
ABORTED_STATUS = "aborted_before_submit"
ALLOWED_COMMIT_STATUSES = {
    SUBMITTING_STATUS,
    "submitted",
    "success",
    ABORTED_STATUS,
    UNKNOWN_STATUS,
} | TERMINAL_HALT_STATUSES

# Explicit transition table. A submitted step can only move forward; success and
# terminal statuses are final. "unknown" can only be reconciled by an explicit
# human-confirmed commit to success/failed with evidence.
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "reserved": {
        SUBMITTING_STATUS,
        "submitted",
        "success",
        ABORTED_STATUS,
        UNKNOWN_STATUS,
    }
    | TERMINAL_HALT_STATUSES,
    SUBMITTING_STATUS: {"submitted", UNKNOWN_STATUS},
    "submitted": {"success", UNKNOWN_STATUS} | TERMINAL_HALT_STATUSES,
    UNKNOWN_STATUS: {"success", "failed"},
    "success": set(),
    ABORTED_STATUS: set(),
    "failed": set(),
    "expired": set(),
    "cancelled": set(),
    "banned": set(),
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _parse_json_object(raw: str | None, flag: str) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{flag} must be valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{flag} must be a JSON object")
    return value


@contextmanager
def _ledger_lock(path: Path) -> Iterator[None]:
    """Serialize load-modify-save on a lock file owned by this ledger."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    fh = open(lock_path, "a+b")
    locked = False
    try:
        fh.seek(0, os.SEEK_END)
        if fh.tell() == 0:
            fh.write(b"\0")
            fh.flush()
            os.fsync(fh.fileno())
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fh.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        f"LEDGER LOCK TIMEOUT: could not lock '{lock_path}' within "
                        f"{LOCK_TIMEOUT_SECONDS:g}s (pid={os.getpid()}). Another ledger "
                        "process is still running; do not retry blindly."
                    )
                time.sleep(0.05)
        yield
    finally:
        if locked:
            try:
                fh.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        fh.close()


def _load_ledger(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": 1, "tasks": {}}
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), dict):
        raise RuntimeError(f"Corrupt ledger structure at {path}")
    try:
        root_version = int(data.get("schema_version", 1))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Corrupt schema_version in ledger at {path}") from exc
    if root_version > SCHEMA_VERSION:
        raise RuntimeError(
            f"Unsupported ledger schema_version={root_version} at {path}; "
            f"this tool supports up to {SCHEMA_VERSION}."
        )
    return data


def _save_ledger_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=".ledger_", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _require_task(data: dict[str, Any], task_id: str) -> dict[str, Any]:
    task = data["tasks"].get(task_id)
    if not isinstance(task, dict):
        raise KeyError(f"Task '{task_id}' not initialized in ledger. Run init-task first.")
    return task


def _task_schema(task: dict[str, Any]) -> int:
    raw = task.get("schema_version", 1)
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Corrupt task schema_version: {raw!r}") from exc


def _non_negative_int(raw: Any, what: str) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Corrupt or invalid {what}: {raw!r}") from exc
    if value < 0:
        raise RuntimeError(f"{what} must be non-negative, got {value}")
    return value


def _step_credits(step: dict[str, Any]) -> int:
    return _non_negative_int(step.get("credits", 0), "step credits")


def _step_actual(step: dict[str, Any]) -> int:
    return _non_negative_int(step.get("actual_credits", 0), "step actual_credits")


def _step_trial_count(step: dict[str, Any]) -> int:
    return _non_negative_int(step.get("trial_count", 0), "step trial_count")


def _step_downloads(step: dict[str, Any]) -> int:
    return _non_negative_int(step.get("downloads", 0), "step downloads")


def _status(step: dict[str, Any]) -> str:
    return str(step.get("status", "")).lower()


def _step_was_submitted(step: dict[str, Any]) -> bool:
    """A true flag is permanent. Submitted/success/unknown also imply it."""
    if step.get("was_submitted") is True:
        return True
    return _status(step) in {"submitted", "success", UNKNOWN_STATUS}


def _steps_dict(task: dict[str, Any]) -> dict[str, Any]:
    steps = task.get("steps")
    if steps is None:
        steps = {}
        task["steps"] = steps
    if not isinstance(steps, dict):
        raise RuntimeError("Corrupt task steps: expected an object")
    return steps


def _recompute_totals(task: dict[str, Any]) -> None:
    steps = _steps_dict(task)
    spent = 0
    reserved = 0
    for step in steps.values():
        if not isinstance(step, dict):
            raise RuntimeError("Corrupt task step: expected an object")
        credits = _step_credits(step)
        actual = _step_actual(step)
        if _step_was_submitted(step):
            # Never let an actual-credit receipt erase potential spend.
            spent += max(credits, actual)
        else:
            spent += actual
            if _status(step) in {"reserved", SUBMITTING_STATUS}:
                reserved += credits

    # A legacy task with no steps may carry totals without step detail. Never
    # silently zero those old reservations / unclear spend.
    if _task_schema(task) < SCHEMA_VERSION and not steps:
        spent = max(spent, _non_negative_int(task.get("spent_credits", 0), "spent_credits"))
        reserved = max(
            reserved,
            _non_negative_int(task.get("reserved_credits", 0), "reserved_credits"),
        )

    task["spent_credits"] = spent
    task["reserved_credits"] = reserved


def _resource_usage(task: dict[str, Any]) -> dict[str, Any]:
    submissions = 0
    downloads = 0
    trial_usage: dict[str, int] = {}
    for step in _steps_dict(task).values():
        if not isinstance(step, dict):
            raise RuntimeError("Corrupt task step: expected an object")
        if not (_step_was_submitted(step) or _status(step) in {"reserved", SUBMITTING_STATUS}):
            continue
        submissions += 1
        trial = str(step.get("trial") or "").strip()
        if trial:
            trial_usage[trial] = trial_usage.get(trial, 0) + _step_trial_count(step)
        downloads += _step_downloads(step)
    return {
        "submissions_used": submissions,
        "trial_usage": trial_usage,
        "downloads_used": downloads,
    }


def _resource_summary(task: dict[str, Any]) -> dict[str, Any]:
    max_submissions = _non_negative_int(task.get("max_submissions", 1), "max_submissions")
    download_cap = _non_negative_int(task.get("download_cap", 0), "download_cap")
    caps_raw = task.get("trial_caps", {})
    if caps_raw is None:
        caps_raw = {}
    if not isinstance(caps_raw, dict):
        raise RuntimeError("Corrupt trial_caps: expected an object")
    trial_caps = {
        str(name): _non_negative_int(count, f"trial_caps[{name}]")
        for name, count in caps_raw.items()
    }
    usage = _resource_usage(task)
    trial_remaining = {
        name: max(0, cap - usage["trial_usage"].get(name, 0))
        for name, cap in trial_caps.items()
    }
    return {
        "enforced": _task_schema(task) >= SCHEMA_VERSION,
        "max_submissions": max_submissions,
        "submissions_used": usage["submissions_used"],
        "submissions_remaining": max(0, max_submissions - usage["submissions_used"]),
        "trial_caps": trial_caps,
        "trial_usage": usage["trial_usage"],
        "trial_remaining": trial_remaining,
        "download_cap": download_cap,
        "downloads_used": usage["downloads_used"],
        "downloads_remaining": max(0, download_cap - usage["downloads_used"]),
    }


def _steps_with_status(task: dict[str, Any], wanted: str) -> list[str]:
    found: list[str] = []
    for key, step in _steps_dict(task).items():
        if isinstance(step, dict) and _status(step) == wanted:
            found.append(str(key))
    return found


def _has_attempt_history(task: dict[str, Any]) -> bool:
    steps = task.get("steps")
    if steps:
        return True
    if task.get("history") or task.get("halt_history"):
        return True
    if task.get("halted"):
        return True
    if _non_negative_int(task.get("spent_credits", 0), "spent_credits") > 0:
        return True
    if _non_negative_int(task.get("reserved_credits", 0), "reserved_credits") > 0:
        return True
    return False


def _append_step_history(
    step: dict[str, Any],
    event: str,
    *,
    from_status: str | None = None,
    to_status: str | None = None,
    **extra: Any,
) -> None:
    history = step.get("history")
    if not isinstance(history, list):
        history = []
        step["history"] = history
    entry: dict[str, Any] = {"at": _now_iso(), "event": event}
    if from_status is not None:
        entry["from"] = from_status
    if to_status is not None:
        entry["to"] = to_status
    for key, value in extra.items():
        if value is not None:
            entry[key] = value
    history.append(entry)


def _halt(task: dict[str, Any], reason: str) -> None:
    task["halted"] = True
    task["halt_reason"] = reason
    history = task.get("halt_history")
    if not isinstance(history, list):
        history = []
        task["halt_history"] = history
    history.append({"at": _now_iso(), "event": "halt", "reason": reason})


def _parse_trial_caps(values: list[str] | None) -> dict[str, int]:
    caps: dict[str, int] = {}
    for item in values or []:
        if "=" not in item:
            raise ValueError("--trial-cap must be NAME=COUNT")
        name, raw_count = item.split("=", 1)
        name = name.strip()
        if not name:
            raise ValueError("--trial-cap requires a non-empty NAME")
        try:
            count = int(raw_count)
        except ValueError as exc:
            raise ValueError(f"--trial-cap COUNT must be an integer: {item!r}") from exc
        if count < 0:
            raise ValueError("--trial-cap COUNT must be non-negative")
        if name in caps:
            raise ValueError(f"Duplicate --trial-cap name: {name}")
        caps[name] = count
    return caps


def cmd_init_task(args: argparse.Namespace) -> dict[str, Any]:
    path = Path(args.ledger)
    with _ledger_lock(path):
        data = _load_ledger(path)
        existing = data["tasks"].get(args.task_id)
        if existing and not args.force:
            raise RuntimeError(
                f"Task '{args.task_id}' already exists in ledger "
                "(pass --force only if safe to reset)."
            )
        if existing and _has_attempt_history(existing):
            raise RuntimeError(
                f"Refusing to reset task '{args.task_id}': it has steps or attempt/halt "
                "history. Old attempts are never erased by init --force."
            )

        budget_cap = _non_negative_int(args.budget_cap, "budget_cap")
        max_submissions = _non_negative_int(args.max_submissions, "max_submissions")
        download_cap = _non_negative_int(args.download_cap, "download_cap")
        trial_caps = _parse_trial_caps(args.trial_cap)
        now = _now_iso()
        task = {
            "task_id": args.task_id,
            "schema_version": SCHEMA_VERSION,
            "region": args.region,
            "budget_cap": budget_cap,
            "max_submissions": max_submissions,
            "trial_caps": trial_caps,
            "download_cap": download_cap,
            "spent_credits": 0,
            "reserved_credits": 0,
            "halted": False,
            "halt_reason": "",
            "note": args.note or "",
            "created_at": now,
            "updated_at": now,
            "halt_history": [],
            "steps": {},
        }
        data["schema_version"] = SCHEMA_VERSION
        data["tasks"][args.task_id] = task
        _save_ledger_atomic(path, data)
    return {"ok": True, "action": "init-task", "task": task}


def cmd_reserve(args: argparse.Namespace) -> dict[str, Any]:
    path = Path(args.ledger)
    metadata = _parse_json_object(args.metadata_json, "--metadata-json")
    with _ledger_lock(path):
        data = _load_ledger(path)
        task = _require_task(data, args.task_id)

        if task.get("halted"):
            raise RuntimeError(
                f"TASK HALTED: '{args.task_id}' is halted ({task.get('halt_reason')}). "
                "Human review required before any new spend."
            )
        unknown_steps = _steps_with_status(task, UNKNOWN_STATUS)
        if unknown_steps:
            raise RuntimeError(
                "UNKNOWN STATE UNRESOLVED: step(s) "
                f"{', '.join(unknown_steps)} are unknown. Reconcile them with an "
                "explicit commit before reserving anything new."
            )
        submitting_steps = _steps_with_status(task, SUBMITTING_STATUS)
        if submitting_steps:
            raise RuntimeError(
                "SUBMISSION IN PROGRESS: step(s) "
                f"{', '.join(submitting_steps)} are submitting. Commit them to "
                "submitted/unknown before reserving anything new."
            )

        steps = _steps_dict(task)
        if args.step_key in steps:
            prev_status = steps[args.step_key].get("status", "?")
            raise RuntimeError(
                f"IDEMPOTENCY BLOCK: step '{args.step_key}' already exists with "
                f"status='{prev_status}'. Same step_key is never overwritten; use a "
                "new step_key only after reviewing the old attempt."
            )

        est = _non_negative_int(args.credits, "credits")
        downloads = _non_negative_int(args.downloads, "downloads")
        trial = str(args.trial or "").strip()
        trial_count_raw = args.trial_count
        if trial:
            trial_count = 1 if trial_count_raw is None else _non_negative_int(
                trial_count_raw, "trial_count"
            )
            if trial_count < 1:
                raise ValueError("trial_count must be >= 1 when --trial is used")
        else:
            if trial_count_raw not in (None, 0):
                raise ValueError("--trial-count requires --trial")
            trial_count = 0

        new_schema = _task_schema(task) >= SCHEMA_VERSION
        if not new_schema and (trial or trial_count or downloads):
            raise RuntimeError(
                "Resource budgets are only enforced for schema_version>=2 tasks. "
                "Create a new task (legacy task history must not be reset)."
            )

        _recompute_totals(task)
        cap = _non_negative_int(task.get("budget_cap", 0), "budget_cap")
        projected = task["spent_credits"] + task["reserved_credits"] + est
        if projected > cap:
            raise RuntimeError(
                f"BUDGET CAP EXCEEDED: projected={projected} > cap={cap} "
                f"(spent={task['spent_credits']}, reserved={task['reserved_credits']}, "
                f"requested={est})."
            )

        resource_summary: dict[str, Any] | None = None
        if new_schema:
            usage = _resource_usage(task)
            max_submissions = _non_negative_int(
                task.get("max_submissions", 1), "max_submissions"
            )
            if usage["submissions_used"] + 1 > max_submissions:
                raise RuntimeError(
                    f"MAX SUBMISSIONS EXCEEDED: possible submissions used="
                    f"{usage['submissions_used']} (reserved/submitting counted), "
                    f"max={max_submissions}."
                )
            if trial:
                trial_caps = task.get("trial_caps") or {}
                if not isinstance(trial_caps, dict) or trial not in trial_caps:
                    configured = sorted(trial_caps) if isinstance(trial_caps, dict) else []
                    raise RuntimeError(
                        f"TRIAL NOT ALLOWED: '{trial}' is not configured. "
                        f"Configured trials={configured}."
                    )
                trial_cap = _non_negative_int(
                    trial_caps.get(trial), f"trial_caps[{trial}]"
                )
                used = usage["trial_usage"].get(trial, 0)
                if used + trial_count > trial_cap:
                    raise RuntimeError(
                        f"TRIAL CAP EXCEEDED: trial='{trial}' used={used}, "
                        f"requested={trial_count}, cap={trial_cap}."
                    )
            if downloads:
                download_cap = _non_negative_int(
                    task.get("download_cap", 0), "download_cap"
                )
                if usage["downloads_used"] + downloads > download_cap:
                    raise RuntimeError(
                        f"DOWNLOAD CAP EXCEEDED: used={usage['downloads_used']}, "
                        f"requested={downloads}, cap={download_cap}."
                    )

        now = _now_iso()
        step: dict[str, Any] = {
            "step_key": args.step_key,
            "operation": args.operation,
            "credits": est,
            "status": "reserved",
            "was_submitted": False,
            "operator_id": "",
            "project_id": "",
            "note": args.note or "",
            "reserved_at": now,
            "updated_at": now,
            "history": [
                {
                    "at": now,
                    "event": "reserved",
                    "to": "reserved",
                    "credits": est,
                }
            ],
        }
        if new_schema:
            step["trial"] = trial
            step["trial_count"] = trial_count
            step["downloads"] = downloads
        if metadata is not None:
            step["metadata"] = metadata
        steps[args.step_key] = step
        _recompute_totals(task)
        resource_summary = _resource_summary(task)
        task["updated_at"] = now
        _save_ledger_atomic(path, data)

    result: dict[str, Any] = {
        "ok": True,
        "action": "reserve",
        "step_key": args.step_key,
        "credits_reserved": est,
        "remaining_budget": cap - task["spent_credits"] - task["reserved_credits"],
    }
    if new_schema and resource_summary is not None:
        result["resource_usage"] = resource_summary
    return result


def cmd_commit(args: argparse.Namespace) -> dict[str, Any]:
    path = Path(args.ledger)
    evidence = _parse_json_object(args.evidence_json, "--evidence-json")
    actual = None if args.actual_credits is None else _non_negative_int(
        args.actual_credits, "actual_credits"
    )
    with _ledger_lock(path):
        data = _load_ledger(path)
        task = _require_task(data, args.task_id)
        steps = _steps_dict(task)
        step = steps.get(args.step_key)
        if not isinstance(step, dict):
            raise KeyError(f"Step '{args.step_key}' not reserved in task '{args.task_id}'.")

        new_status = str(args.status).lower()
        if new_status not in ALLOWED_COMMIT_STATUSES:
            raise ValueError(f"Invalid commit status: {args.status}")
        current_status = _status(step)
        allowed = ALLOWED_TRANSITIONS.get(current_status)
        if allowed is None:
            raise RuntimeError(
                f"Unsupported current status '{current_status}' for step "
                f"'{args.step_key}'; manual review required."
            )
        if new_status == current_status:
            raise RuntimeError(
                f"STATE OVERWRITE BLOCKED: step '{args.step_key}' is already "
                f"'{current_status}'. No-op/repeat commits are not allowed."
            )
        if new_status not in allowed:
            raise RuntimeError(
                f"INVALID STATE TRANSITION: {current_status} -> {new_status} for "
                f"step '{args.step_key}'. Submitted/history states cannot be "
                "rewound; success/terminal states are final."
            )

        if current_status == UNKNOWN_STATUS:
            if not args.reconcile_unknown:
                raise RuntimeError(
                    "UNKNOWN RECONCILE REQUIRED: unknown -> success/failed needs "
                    "--reconcile-unknown plus --evidence-json."
                )
            if evidence is None:
                raise RuntimeError(
                    "UNKNOWN RECONCILE REQUIRES --evidence-json: record the human "
                    "readback that proves the real final state."
                )
        elif args.reconcile_unknown:
            raise RuntimeError(
                "--reconcile-unknown is only valid when the current status is unknown."
            )

        if step.get("was_submitted") is True and new_status == ABORTED_STATUS:
            raise RuntimeError(
                "SUBMITTED STEP CANNOT BE ABORTED: was_submitted=true is permanent "
                "and its credits remain spent."
            )
        if new_status == SUBMITTING_STATUS:
            if task.get('halted') or any(_status(other)==UNKNOWN_STATUS for key,other in steps.items() if key!=args.step_key and isinstance(other,dict)):
                raise RuntimeError('TASK HALTED OR UNKNOWN: an existing reservation cannot begin a new submission.')
            if actual is not None:
                raise ValueError("--actual-credits is not valid for status=submitting")
            others = [
                str(key)
                for key, other in steps.items()
                if key != args.step_key
                and isinstance(other, dict)
                and _status(other) == SUBMITTING_STATUS
            ]
            if others:
                raise RuntimeError(
                    "SUBMISSION IN PROGRESS: step(s) "
                    f"{', '.join(others)} are already submitting. Resolve them "
                    "before starting another click."
                )

        reserved_for_step = _step_credits(step)
        step["status"] = new_status
        if new_status in {"submitted", "success", UNKNOWN_STATUS}:
            step["was_submitted"] = True
        if args.operator_id:
            step["operator_id"] = args.operator_id
        if args.project_id:
            step["project_id"] = args.project_id
        if args.error:
            step["error"] = args.error
        if actual is not None:
            step["actual_credits"] = actual
            if actual > reserved_for_step:
                step["actual_exceeds_reserved"] = True
            elif actual < reserved_for_step:
                step["actual_below_reserved"] = True
        if evidence is not None:
            step["evidence"] = evidence
        if args.reconcile_unknown:
            step["reconciled_unknown"] = True
            step["reconciled_at"] = _now_iso()
        step["updated_at"] = _now_iso()
        _append_step_history(
            step,
            "reconcile" if args.reconcile_unknown else "commit",
            from_status=current_status,
            to_status=new_status,
            actual_credits=actual,
            evidence_recorded=evidence is not None,
            operator_id=args.operator_id or None,
        )

        if new_status in TERMINAL_HALT_STATUSES or new_status == UNKNOWN_STATUS:
            _halt(
                task,
                f"step '{args.step_key}' ended with '{new_status}': "
                f"{args.error or 'no detail'}",
            )
        if actual is not None and actual > reserved_for_step:
            _halt(
                task,
                f"ACTUAL CREDITS EXCEED RESERVED: step '{args.step_key}' "
                f"actual={actual} > reserved={reserved_for_step}.",
            )

        _recompute_totals(task)
        resource_summary = _resource_summary(task)
        cap_for_output = int(task.get("budget_cap", 0))
        task["updated_at"] = _now_iso()
        _save_ledger_atomic(path, data)

    result: dict[str, Any] = {
        "ok": True,
        "action": "commit",
        "step_key": args.step_key,
        "status": new_status,
        "halted": bool(task.get("halted", False)),
        "spent_credits": task["spent_credits"],
        "reserved_credits": task["reserved_credits"],
        "remaining_budget": cap_for_output
        - task["spent_credits"]
        - task["reserved_credits"],
    }
    if actual is not None:
        result["actual_credits"] = actual
    if _task_schema(task) >= SCHEMA_VERSION:
        result["resource_usage"] = resource_summary
    return result


def cmd_clear_halt(args: argparse.Namespace) -> dict[str, Any]:
    path = Path(args.ledger)
    with _ledger_lock(path):
        data = _load_ledger(path)
        task = _require_task(data, args.task_id)
        if not args.human_review:
            raise RuntimeError(
                "HUMAN REVIEW REQUIRED: clear-halt needs --human-review. It only "
                "clears the halt flag; old step keys are never retryable."
            )
        reason = str(args.reason or "").strip()
        if not reason:
            raise ValueError("clear-halt --reason must not be empty")
        unknown_steps = _steps_with_status(task, UNKNOWN_STATUS)
        if unknown_steps:
            raise RuntimeError(
                "UNKNOWN STATE UNRESOLVED: step(s) "
                f"{', '.join(unknown_steps)} must be explicitly reconciled to "
                "success/failed before clear-halt."
            )
        submitting_steps = _steps_with_status(task, SUBMITTING_STATUS)
        if submitting_steps:
            raise RuntimeError(
                "SUBMISSION IN PROGRESS: step(s) "
                f"{', '.join(submitting_steps)} must become submitted/unknown "
                "before clear-halt."
            )

        now = _now_iso()
        previous_reason = str(task.get("halt_reason", ""))
        history = task.get("halt_history")
        if not isinstance(history, list):
            history = []
            task["halt_history"] = history
        history.append(
            {
                "at": now,
                "event": "clear-halt",
                "previous_halt_reason": previous_reason,
                "reason": reason,
                "human_review": True,
            }
        )
        task["halted"] = False
        task["halt_reason"] = f"Cleared by human approval ({now}): {reason}"
        task["updated_at"] = now
        _save_ledger_atomic(path, data)
    return {
        "ok": True,
        "action": "clear-halt",
        "task_id": args.task_id,
        "halted": False,
    }


def cmd_status(args: argparse.Namespace) -> dict[str, Any]:
    path = Path(args.ledger)
    data = _load_ledger(path)
    task = _require_task(data, args.task_id)
    _recompute_totals(task)
    cap = int(task.get("budget_cap", 0))
    steps = _steps_dict(task)
    unknown_steps = _steps_with_status(task, UNKNOWN_STATUS)
    submitting_steps = _steps_with_status(task, SUBMITTING_STATUS)
    return {
        "ok": True,
        "task_id": args.task_id,
        "schema_version": _task_schema(task),
        "region": task.get('region','cn'),
        "budget_cap": cap,
        "spent_credits": task["spent_credits"],
        "reserved_credits": task["reserved_credits"],
        "remaining_budget": cap - task["spent_credits"] - task["reserved_credits"],
        "halted": bool(task.get("halted", False)),
        "halt_reason": task.get("halt_reason", ""),
        "unresolved_unknown": unknown_steps,
        "submitting": submitting_steps,
        "resource_usage": _resource_summary(task),
        "steps": steps,
    }


def cmd_estimate(args: argparse.Namespace) -> dict[str, Any]:
    if args.kind == "generate":
        est = estimate_generate(
            tier=args.tier,
            hd_version=args.hd_version,
            lp_version=args.lp_version,
            mode=args.mode,
            batch_count=args.batch_count,
            variations=args.variations,
            quad=args.quad,
            texture=not args.no_texture,
            pbr=args.pbr,
            texture_quality=args.texture_quality,
            geometry_quality=args.geometry_quality,
            generate_parts=args.generate_parts,
            smart_poly=args.smart_poly,
            smart_mesh_p2_trial=args.smart_mesh_p2_trial,
            ultra_texture_trial=args.ultra_texture_trial,
        )
    else:
        est = estimate_postprocess(
            args.operation,
            quad=not args.triangle,
            smart_poly=args.smart_poly,
            texture_quality=args.texture_quality,
            use_style=args.use_style,
            upscale_extreme=args.upscale_extreme,
            is_uv_retry=args.is_uv_retry,
            is_owner=not args.non_owner,
            uv_generate_trial=args.uv_generate_trial,
            upscaler_trial=args.upscaler_trial,
            brush_mode=args.brush_mode,
        )
    return {"ok": True, "estimate": est.to_dict()}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Tripo Studio pre-spend budget cap & idempotency ledger")
    p.add_argument("--ledger", default=str(DEFAULT_LEDGER_PATH), help="Path to ledger.json")
    sub = p.add_subparsers(dest="subcmd", required=True)

    p_init = sub.add_parser("init-task", help="Initialize a task with strict credit budget cap")
    p_init.add_argument("--task-id", required=True)
    p_init.add_argument("--budget-cap", required=True, type=int)
    p_init.add_argument("--region", default="cn", choices=["cn", "global"])
    p_init.add_argument("--note", default="")
    p_init.add_argument("--force", action="store_true")
    p_init.add_argument("--max-submissions", type=int, default=1)
    p_init.add_argument(
        "--trial-cap",
        action="append",
        default=[],
        metavar="NAME=COUNT",
        help="Repeatable trial allowance; default empty",
    )
    p_init.add_argument("--download-cap", type=int, default=0)

    p_res = sub.add_parser("reserve", help="Atomically reserve credits under unique idempotency step-key")
    p_res.add_argument("--task-id", required=True)
    p_res.add_argument("--step-key", required=True)
    p_res.add_argument("--operation", required=True)
    p_res.add_argument("--credits", required=True, type=int)
    p_res.add_argument("--note", default="")
    p_res.add_argument("--trial", default=None)
    p_res.add_argument("--trial-count", type=int, default=None)
    p_res.add_argument("--downloads", type=int, default=0)
    p_res.add_argument("--metadata-json", default=None)

    p_com = sub.add_parser("commit", help="Record submission / completion / failure (halts on failed/unknown)")
    p_com.add_argument("--task-id", required=True)
    p_com.add_argument("--step-key", required=True)
    p_com.add_argument("--status", required=True)
    p_com.add_argument("--operator-id", default="")
    p_com.add_argument("--project-id", default="")
    p_com.add_argument("--error", default="")
    p_com.add_argument("--actual-credits", type=int, default=None)
    p_com.add_argument("--evidence-json", default=None)
    p_com.add_argument("--reconcile-unknown", action="store_true")

    p_clr = sub.add_parser("clear-halt", help="Clear halt flag ONLY after human intervention")
    p_clr.add_argument("--task-id", required=True)
    p_clr.add_argument("--reason", required=True)
    p_clr.add_argument("--human-review", action="store_true")

    p_st = sub.add_parser("status", help="Inspect current ledger status")
    p_st.add_argument("--task-id", required=True)

    p_est = sub.add_parser("estimate", help="Compute exact Studio credit estimate (0 network cost)")
    p_est.add_argument("--kind", required=True, choices=["generate", "postprocess"])
    p_est.add_argument("--tier", default="hd", choices=["hd", "smartmesh"])
    p_est.add_argument("--hd-version", default="v3.1-20260211")
    p_est.add_argument("--lp-version", default="Nexus-v2.0-20260801")
    p_est.add_argument("--mode", default="single", choices=["single", "multiview", "batch", "text"])
    p_est.add_argument("--batch-count", type=int, default=1)
    p_est.add_argument('--variations',type=int,choices=[1,2,4],default=1)
    p_est.add_argument("--quad", action="store_true")
    p_est.add_argument("--triangle", action="store_true")
    p_est.add_argument("--no-texture", action="store_true")
    p_est.add_argument("--pbr", action="store_true")
    p_est.add_argument("--texture-quality", default="detailed", choices=["standard", "detailed", "extreme"])
    p_est.add_argument("--geometry-quality", default="detailed", choices=["standard", "detailed"])
    p_est.add_argument("--generate-parts", action="store_true")
    p_est.add_argument("--smart-poly", action="store_true")
    p_est.add_argument("--smart-mesh-p2-trial", type=int, default=0)
    p_est.add_argument("--ultra-texture-trial", type=int, default=0)
    p_est.add_argument("--operation", default="segmentation")
    p_est.add_argument("--use-style", action="store_true")
    p_est.add_argument("--upscale-extreme", action="store_true")
    p_est.add_argument("--is-uv-retry", action="store_true")
    p_est.add_argument("--non-owner", action="store_true")
    p_est.add_argument("--uv-generate-trial", type=int, default=0)
    p_est.add_argument("--upscaler-trial", type=int, default=0)
    p_est.add_argument("--brush-mode", action="store_true")

    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    dispatch = {
        "init-task": cmd_init_task,
        "reserve": cmd_reserve,
        "commit": cmd_commit,
        "clear-halt": cmd_clear_halt,
        "status": cmd_status,
        "estimate": cmd_estimate,
    }
    try:
        result = dispatch[args.subcmd](args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
