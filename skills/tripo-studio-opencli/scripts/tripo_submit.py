#!/usr/bin/env python3
"""Bounded, exactly-once submission primitive for Tripo Studio web flows.

The caller owns the OpenCLI browser session/page and the ledger.  This module
only sequences three callback operations:

* ``call("network")``                    -- baseline / post-click cache list
* ``call("click", submit_target)``       -- one structured click, never eval
* ``call("network", "--detail", key)``   -- one cached response body

``decode(raw)`` must turn CLI text into the OpenCLI JSON envelope.  A mapping
or list is also accepted directly, which keeps the callback easy to test.
``commit(status, receipt_or_evidence)`` must synchronously persist the ledger
transition and raise (or return ``{"ok": false}``) on failure.

Only the exact endpoint path and API origin supplied by the caller can match a
network entry.  A submission that may have happened but cannot be proven is
committed as ``unknown`` and raised as :class:`SubmissionUnknown`; this module
never clicks a second time.  A baseline network failure (provably before any
click) is surfaced as :class:`SubmissionNotAttempted` so the parent can commit
``aborted_before_submit`` while it still owns reservation/preconfiguration.
Local validation and intent-commit failures have their own exceptions and also
never click.

No credit estimate or actual-credit value is derived here.  Pricing and
reconciliation stay in the parent.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any, NoReturn
from urllib.parse import urlsplit

__all__ = [
    "ALLOWED_UNKNOWN_EVIDENCE_KEYS",
    "DEFAULT_API_ORIGIN_CN",
    "DEFAULT_API_ORIGIN_GLOBAL",
    "NetworkEnvelopeError",
    "ReceiptError",
    "SingleSubmission",
    "SubmissionCommitError",
    "SubmissionConfigError",
    "SubmissionError",
    "SubmissionNotAttempted",
    "SubmissionUnknown",
    "match_network_entry",
    "network_entry_key",
    "parse_network_entries",
    "parse_network_snapshot",
    "parse_receipt",
    "parse_timestamp",
]


# These are the only caller-visible unknown/evidence keys.  In particular, a
# response URL, body, message, or exception text is never part of evidence.
ALLOWED_UNKNOWN_EVIDENCE_KEYS = frozenset(
    {
        "reason",
        "endpoint",
        "network_key",
        "network_keys",
        "http_status",
        "code",
        "count",
        "expected_count",
        "operator_ids",
        "project_ids",
        "task_ids",
        "index",
        "field",
    }
)

_SAFE_CODE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._:@+\-/=]{1,256}$")
_SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,300}(?:#\d{1,6})?$")

DEFAULT_API_ORIGIN_CN = "https://api-cn.tripo3d.com"
DEFAULT_API_ORIGIN_GLOBAL = "https://api.tripo3d.ai"


def arm_capture(call,decode):
    """Public OpenCLI wait arms capture on the already-navigated owned page.

    The impossible URL is a regex only: no request is sent. A timeout is the
    expected acknowledgement; every other outcome refuses submission.
    """
    try:
        raw=call('wait','xhr','__tripo_capture_arm_never_match__','--timeout','1')
    except Exception as exc:
        if getattr(exc,'code',None)=='xhr_not_seen':return
        raise SubmissionNotAttempted('network_capture_arm_failed') from None
    try:result=raw if isinstance(raw,Mapping) else decode(raw)
    except Exception:raise SubmissionNotAttempted('network_capture_arm_invalid') from None
    if not isinstance(result,Mapping) or not isinstance(result.get('error'),Mapping) or result['error'].get('code')!='xhr_not_seen':
        raise SubmissionNotAttempted('network_capture_arm_unconfirmed')


class SubmissionError(RuntimeError):
    """Base class for submission sequencing failures."""


class SubmissionConfigError(SubmissionError, ValueError):
    """Invalid local input; no browser operation was attempted."""

    click_attempted = False

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"submission config rejected: reason={reason}")


class SubmissionNotAttempted(SubmissionError):
    """The click was provably never attempted.

    The parent owns ``aborted_before_submit`` (and reservation/preconfiguration
    failures).  This module deliberately does not write that ledger status.
    """

    click_attempted = False

    def __init__(
        self,
        reason: str,
        *,
        endpoint: str = "",
        expected_count: int | None = None,
    ) -> None:
        self.reason = reason
        evidence: dict[str, Any] = {"reason": reason}
        if endpoint:
            evidence["endpoint"] = endpoint
        if expected_count is not None:
            evidence["expected_count"] = expected_count
        self.evidence = evidence
        super().__init__(
            "submission NOT attempted: "
            f"reason={reason} endpoint={endpoint or '-'}"
        )


class SubmissionCommitError(SubmissionError):
    """The parent's ledger callback rejected or raised for a commit."""

    def __init__(
        self,
        status: str,
        *,
        evidence: Mapping[str, Any],
        click_attempted: bool,
        cause_type: str | None = None,
    ) -> None:
        self.status = status
        self.evidence = dict(evidence)
        self.click_attempted = click_attempted
        self.cause_type = cause_type
        super().__init__(
            "ledger commit failed: "
            f"status={status} click_attempted={click_attempted} "
            f"cause={cause_type or 'callback'}"
        )


class SubmissionUnknown(SubmissionError):
    """A click happened but the outcome cannot be safely established."""

    click_attempted = True

    def __init__(
        self,
        reason: str,
        evidence: Mapping[str, Any],
        *,
        commit_error_type: str | None = None,
    ) -> None:
        self.reason = reason
        self.evidence = dict(evidence)
        self.commit_error_type = commit_error_type
        parts = [f"reason={reason}"]
        for key in ("endpoint", "network_key", "http_status", "code", "count"):
            value = self.evidence.get(key)
            if value not in (None, ""):
                parts.append(f"{key}={value}")
        super().__init__("submission outcome UNKNOWN (halt): " + " ".join(parts))


class NetworkEnvelopeError(ValueError):
    """The OpenCLI network list envelope has an unsupported shape."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"unsupported network list envelope: {reason}")


class ReceiptError(ValueError):
    """A submit response body is incomplete, non-zero, or inconsistent."""

    def __init__(
        self,
        reason: str,
        *,
        code: Any = None,
        count: int | None = None,
        expected_count: int | None = None,
        index: int | None = None,
        field: str | None = None,
    ) -> None:
        self.reason = reason
        details: dict[str, Any] = {}
        safe_code = _safe_code(code)
        if safe_code is not None:
            details["code"] = safe_code
        if isinstance(count, int) and not isinstance(count, bool):
            details["count"] = count
        if isinstance(expected_count, int) and not isinstance(expected_count, bool):
            details["expected_count"] = expected_count
        if isinstance(index, int) and not isinstance(index, bool):
            details["index"] = index
        if field in {"operator_id", "project_id"}:
            details["field"] = field
        self.details = details
        super().__init__(f"receipt invalid: reason={reason}")


def _safe_code(value: Any) -> int | str | None:
    """Return a short scalar code that cannot carry a URL/body payload."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if _SAFE_CODE_RE.fullmatch(text):
            return text
    return None


def _is_zero_code(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value == 0
    if isinstance(value, str):
        return value.strip() == "0"
    return False


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text and (text.isdigit() or (text.startswith("-") and text[1:].isdigit())):
            try:
                return int(text)
            except ValueError:
                return None
    return None


def _safe_id(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        return None
    if not _SAFE_ID_RE.fullmatch(text):
        return None
    lowered = text.lower()
    if (
        "://" in text
        or text.startswith("//")
        or lowered.startswith(("http:", "https:"))
    ):
        return None
    return text


def parse_timestamp(value: Any) -> float | None:
    """Normalize a CLI timestamp to UTC epoch seconds.

    The actual network envelope uses ``captured_at`` and per-entry
    ``timestamp`` fields, but their concrete serialization is not documented.
    ISO-8601 strings and numeric epoch seconds/milliseconds/microseconds/
    nanoseconds are accepted without persisting the original value.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, datetime):
        moment = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        try:
            return moment.timestamp()
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            iso = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
            try:
                moment = datetime.fromisoformat(iso)
            except ValueError:
                return None
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            try:
                return moment.timestamp()
            except (OverflowError, OSError, ValueError):
                return None
    else:
        return None
    if not math.isfinite(number):
        return None
    magnitude = abs(number)
    if magnitude >= 1e17:
        number /= 1e9
    elif magnitude >= 1e14:
        number /= 1e6
    elif magnitude >= 1e11:
        number /= 1e3
    return number


def _redacted_key(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if _SAFE_KEY_RE.fullmatch(value):
        return value
    digest = hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()
    return f"sha256:{digest[:16]}"


def _endpoint_path(endpoint: Any) -> str:
    """Validate a caller-supplied endpoint as an exact absolute path."""
    if not isinstance(endpoint, str) or not endpoint:
        raise SubmissionConfigError("endpoint_missing")
    if any(ch.isspace() or ord(ch) < 32 for ch in endpoint):
        raise SubmissionConfigError("endpoint_contains_whitespace")
    if any(ch in endpoint for ch in "?#"):
        raise SubmissionConfigError("endpoint_must_not_contain_query_or_fragment")
    if "\\" in endpoint:
        raise SubmissionConfigError("endpoint_must_use_forward_slashes")
    try:
        parts = urlsplit(endpoint)
    except ValueError:
        raise SubmissionConfigError("endpoint_path_invalid") from None
    if parts.scheme or parts.netloc:
        raise SubmissionConfigError("endpoint_must_be_a_path_not_a_url")
    if not endpoint.startswith("/") or endpoint.startswith("//"):
        raise SubmissionConfigError("endpoint_must_start_with_one_slash")
    if parts.path != endpoint:
        raise SubmissionConfigError("endpoint_path_invalid")
    return endpoint


def _origin_key(api_origin: Any) -> tuple[str, str, int]:
    """Normalize scheme/host/port; reject paths, queries, userinfo."""
    if not isinstance(api_origin, str) or not api_origin.strip():
        raise SubmissionConfigError("api_origin_missing")
    try:
        parts = urlsplit(api_origin.strip())
        scheme = parts.scheme.lower()
        host = parts.hostname
        port = parts.port
    except ValueError:
        raise SubmissionConfigError("api_origin_invalid") from None
    if scheme not in {"http", "https"} or not host:
        raise SubmissionConfigError("api_origin_invalid")
    if parts.username or parts.password:
        raise SubmissionConfigError("api_origin_must_not_contain_userinfo")
    if parts.path not in {"", "/"} or parts.query or parts.fragment:
        raise SubmissionConfigError("api_origin_must_be_origin_only")
    if port is None:
        port = 443 if scheme == "https" else 80
    return scheme, host.lower().rstrip("."), port


def _url_origin_and_path(url: Any) -> tuple[tuple[str, str, int], str] | None:
    if not isinstance(url, str) or not url:
        return None
    try:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if scheme not in {"http", "https"} or not host:
        return None
    if parts.username or parts.password:
        return None
    if port is None:
        port = 443 if scheme == "https" else 80
    return (scheme, host.lower().rstrip("."), port), parts.path


def parse_network_entries(payload: Any) -> list[Any]:
    """Return the entry list from ``entries`` or ``requests`` envelopes.

    OpenCLI documentation shows a bare list; the integration contract may also
    use either wrapper name.  Only those two wrappers are accepted; arbitrary
    mappings are not searched for request-like objects.

    Entries are transient raw browser data and may contain signed URLs.  Callers
    must never log or persist this return value.
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, Mapping):
        if "entries" in payload:
            entries = payload["entries"]
        elif "requests" in payload:
            entries = payload["requests"]
        else:
            raise NetworkEnvelopeError("missing_entries_or_requests")
        if not isinstance(entries, list):
            raise NetworkEnvelopeError("entries_not_list")
        return entries
    raise NetworkEnvelopeError("not_list_or_object")


def parse_network_snapshot(payload: Any) -> dict[str, Any]:
    """Return entries plus a normalized ``captured_at`` when supplied.

    The actual browser network envelope is
    ``{session, captured_at, count, entries, detail_hint}``.  ``session``,
    ``count`` and ``detail_hint`` are intentionally ignored.  Legacy bare-list
    and ``requests`` envelopes remain readable; ``captured_at`` is None when it
    is not supplied.  The returned ``entries`` are transient and may contain
    signed URLs; callers must never persist them.
    """
    entries = parse_network_entries(payload)
    captured_at: float | None = None
    if isinstance(payload, Mapping) and "captured_at" in payload:
        raw_captured_at = payload.get("captured_at")
        if raw_captured_at not in (None, ""):
            captured_at = parse_timestamp(raw_captured_at)
            if captured_at is None:
                raise NetworkEnvelopeError("captured_at_invalid")
    return {"entries": entries, "captured_at": captured_at}


def _baseline_floor(snapshot: Mapping[str, Any]) -> float | None:
    """Prefer snapshot captured_at; otherwise use the newest baseline entry."""
    captured_at = snapshot.get("captured_at")
    if captured_at is not None:
        return captured_at
    timestamps: list[float] = []
    entries = snapshot.get("entries")
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            parsed = parse_timestamp(entry.get("timestamp"))
            if parsed is not None:
                timestamps.append(parsed)
    return max(timestamps) if timestamps else None


def network_entry_key(entry: Any) -> str | None:
    """Return the cache key of one network entry, if it is a non-empty string."""
    if not isinstance(entry, Mapping):
        return None
    key = entry.get("key")
    if isinstance(key, str) and key:
        return key
    return None


def _entry_matches(
    entry: Any,
    origin: tuple[str, str, int],
    endpoint_path: str,
) -> bool:
    if not isinstance(entry, Mapping):
        return False
    method = entry.get("method")
    if not isinstance(method, str) or method.upper() != "POST":
        return False
    parsed = _url_origin_and_path(entry.get("url"))
    if parsed is None:
        return False
    entry_origin, entry_path = parsed
    return entry_origin == origin and entry_path == endpoint_path


def match_network_entry(entry: Any, api_origin: str, endpoint: str) -> bool:
    """Strict POST + origin + exact path matcher used for new cache keys."""
    origin = _origin_key(api_origin)
    endpoint_path = _endpoint_path(endpoint)
    return _entry_matches(entry, origin, endpoint_path)


def _receipt_items(data: Any) -> tuple[list[Any], Any]:
    """Extract only explicit response shapes; never recursively hunt for IDs."""
    if isinstance(data, Mapping):
        if 'variations' in data:
            variants=data['variations']
            if not isinstance(variants,list) or not variants:raise ReceiptError('variations_empty_or_invalid')
            if not all(isinstance(v,Mapping) and type(v.get('index')) is int for v in variants):raise ReceiptError('variation_index_invalid')
            variants=sorted(variants,key=lambda v:v['index'])
            if [v['index'] for v in variants]!=list(range(len(variants))):raise ReceiptError('variation_indices_incomplete')
            if not all(v.get('accepted') is True for v in variants):raise ReceiptError('variation_rejected')
            return variants,data.get('project_id')
        if "tasks" in data:
            tasks = data["tasks"]
            if not isinstance(tasks, list):
                raise ReceiptError("tasks_not_list")
            if not tasks:
                raise ReceiptError("tasks_empty")
            return tasks, data.get("project_id")
        return [data], None
    if isinstance(data, list):
        if not data:
            raise ReceiptError("data_empty")
        return data, None
    raise ReceiptError("data_invalid")


def parse_receipt(
    body: Any,
    expected_count: int,
    expected_project_id: str | None = None,
) -> dict[str, Any]:
    """Validate a submit response and return redacted IDs only.

    Supported explicit shapes::

        {"code": 0, "data": {"operator_id": "...", "project_id": "..."}}
        {"code": 0, "data": [{"operator_id": "...", "project_id": "..."}, ...]}
        {"code": 0, "data": {"project_id": "...", "tasks": [{...}, ...]}}

    An operation id may be supplied as ``operator_id`` or, matching the
    reference driver, as ``task_id``.  For ``data.tasks`` a task may inherit
    ``data.project_id``.  If ``expected_project_id`` is supplied, every
    received project id (including a ``data.project_id`` container) must match
    it, and a missing project id is filled from it.  Without it, every item
    must return a project id (generate behavior).  No other nested objects are
    traversed, and no response fields besides IDs/code/count are returned.
    Credit/price fields and signed URLs are intentionally ignored.
    """
    if (
        isinstance(expected_count, bool)
        or not isinstance(expected_count, int)
        or expected_count < 1
    ):
        raise ValueError("expected_count must be a positive integer")
    expected_project: str | None = None
    if expected_project_id is not None:
        expected_project = _safe_id(expected_project_id)
        if expected_project is None:
            raise ValueError("expected_project_id must be a non-empty safe ID")
    if not isinstance(body, Mapping):
        raise ReceiptError("body_not_object")
    if "code" not in body:
        raise ReceiptError("code_missing")
    if not _is_zero_code(body.get("code")):
        raise ReceiptError("non_zero_code", code=body.get("code"))
    if "data" not in body:
        raise ReceiptError("data_missing")

    items, inherited_project_raw = _receipt_items(body.get("data"))
    container_project: str | None = None
    if inherited_project_raw is not None and inherited_project_raw != "":
        container_project = _safe_id(inherited_project_raw)
        if container_project is None:
            raise ReceiptError("ids_incomplete", field="project_id")
        if expected_project is not None and container_project != expected_project:
            raise ReceiptError("project_id_mismatch", field="project_id")
    elif expected_project is not None:
        container_project = expected_project

    normalized: list[dict[str, str]] = []
    seen_operator_ids: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise ReceiptError("task_not_object", index=index)
        operator_id = _safe_id(item.get("operator_id"))
        if operator_id is None:
            operator_id = _safe_id(item.get("task_id"))
        raw_project_id = item.get("project_id")
        if raw_project_id is None or raw_project_id == "":
            project_id = container_project or expected_project
        else:
            project_id = _safe_id(raw_project_id)
            if project_id is not None and expected_project is not None:
                if project_id != expected_project:
                    raise ReceiptError(
                        "project_id_mismatch",
                        index=index,
                        field="project_id",
                    )
        if operator_id is None:
            raise ReceiptError("ids_incomplete", index=index, field="operator_id")
        if project_id is None:
            raise ReceiptError("ids_incomplete", index=index, field="project_id")
        if operator_id in seen_operator_ids:
            raise ReceiptError("duplicate_operator_id", index=index)
        seen_operator_ids.add(operator_id)

        normalized_item = {
            "operator_id": operator_id,
            "project_id": project_id,
        }
        task_id = _safe_id(item.get("task_id"))
        if task_id is not None:
            normalized_item["task_id"] = task_id
        normalized.append(normalized_item)

    count = len(normalized)
    if count != expected_count:
        raise ReceiptError(
            "count_mismatch",
            count=count,
            expected_count=expected_count,
        )

    return {
        "code": 0,
        "count": count,
        "items": normalized,
        "operator_ids": [item["operator_id"] for item in normalized],
        "project_ids": [item["project_id"] for item in normalized],
        "task_ids": [
            item["task_id"] for item in normalized if "task_id" in item
        ],
    }


def _partial_receipt_ids(body):
    """Keep known IDs on partial acceptance, without persisting any raw body."""
    data=body.get('data') if isinstance(body,Mapping) else None
    parent=data.get('project_id') if isinstance(data,Mapping) else None
    if isinstance(data,Mapping):
        items=data.get('variations',data.get('tasks',[data]))
    else:items=data
    if not isinstance(items,list):return {}
    ops=[];projects=[]
    for item in items[:64]:
        if not isinstance(item,Mapping):continue
        op=_safe_id(item.get('operator_id') or item.get('task_id'))
        project=_safe_id(item.get('project_id') or parent)
        if op and op not in ops:ops.append(op)
        if project and project not in projects:projects.append(project)
    return {'operator_ids':ops,'project_ids':projects}


class SingleSubmission:
    """Sequence one baseline read, one intent commit, one click, one receipt."""

    def __init__(
        self,
        call: Callable[..., Any],
        decode: Callable[[str], Any],
        commit: Callable[[str, Mapping[str, Any]], Any],
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        poll_interval: float = 0.5,
    ) -> None:
        for name, callback in (
            ("call", call),
            ("decode", decode),
            ("commit", commit),
            ("sleep", sleep),
            ("clock", clock),
        ):
            if not callable(callback):
                raise TypeError(f"{name} must be callable")
        if (
            isinstance(poll_interval, bool)
            or not isinstance(poll_interval, (int, float))
            or not math.isfinite(float(poll_interval))
            or float(poll_interval) <= 0
        ):
            raise ValueError("poll_interval must be a positive finite number")
        self._call = call
        self._decode = decode
        self._commit = commit
        self._sleep = sleep
        self._clock = clock
        self._poll_interval = float(poll_interval)

    def _decode_output(self, raw: Any) -> Any:
        if isinstance(raw, (Mapping, list)):
            return raw
        if not isinstance(raw, str):
            raise TypeError("CLI output must be text or an already-decoded object")
        return self._decode(raw)

    def _commit_once(
        self,
        status: str,
        evidence: Mapping[str, Any],
        *,
        click_attempted: bool,
    ) -> Any:
        try:
            result = self._commit(status, evidence)
        except Exception as exc:
            raise SubmissionCommitError(
                status,
                evidence=evidence,
                click_attempted=click_attempted,
                cause_type=type(exc).__name__,
            ) from None
        rejected = result is False
        if isinstance(result, Mapping):
            rejected = rejected or result.get("ok") is False or bool(result.get("error"))
        if rejected:
            raise SubmissionCommitError(
                status,
                evidence=evidence,
                click_attempted=click_attempted,
                cause_type="rejected",
            )
        return result

    def _halt_unknown(
        self,
        reason: str,
        *,
        endpoint: str,
        expected_count: int,
        key: Any = None,
        http_status: Any = None,
        code: Any = None,
        extra: Mapping[str, Any] | None = None,
    ) -> NoReturn:
        evidence: dict[str, Any] = {
            "reason": reason,
            "endpoint": endpoint,
            "expected_count": expected_count,
        }
        redacted_key = _redacted_key(key)
        if redacted_key is not None:
            evidence["network_key"] = redacted_key
        status = _coerce_int(http_status)
        if status is not None:
            evidence["http_status"] = status
        safe_code = _safe_code(code)
        if safe_code is not None:
            evidence["code"] = safe_code

        for extra_key, extra_value in (extra or {}).items():
            if extra_key not in ALLOWED_UNKNOWN_EVIDENCE_KEYS:
                continue
            if extra_key == "network_keys" and isinstance(extra_value, (list, tuple)):
                keys = []
                for item in extra_value:
                    redacted = _redacted_key(item)
                    if redacted is not None and redacted not in keys:
                        keys.append(redacted)
                if keys:
                    evidence["network_keys"] = keys[:8]
            elif extra_key in {"count", "index"}:
                value = _coerce_int(extra_value)
                if value is not None:
                    evidence[extra_key] = value
            elif extra_key == "expected_count":
                value = _coerce_int(extra_value)
                if value is not None:
                    evidence[extra_key] = value
            elif extra_key in {"operator_ids", "project_ids", "task_ids"}:
                if isinstance(extra_value, (list, tuple)):
                    ids = []
                    for item in extra_value:
                        safe_id = _safe_id(item)
                        if safe_id is not None and safe_id not in ids:
                            ids.append(safe_id)
                    if ids:
                        evidence[extra_key] = ids[:16]
            elif extra_key == "field" and extra_value in {"operator_id", "project_id"}:
                evidence["field"] = extra_value

        commit_error_type: str | None = None
        try:
            self._commit_once("unknown", evidence, click_attempted=True)
        except SubmissionCommitError as exc:
            commit_error_type = exc.cause_type or "callback"
        raise SubmissionUnknown(
            reason,
            evidence,
            commit_error_type=commit_error_type,
        ) from None

    def run(
        self,
        endpoint: str,
        submit_target: str,
        expected_count: int = 1,
        api_origin: str | None = DEFAULT_API_ORIGIN_CN,
        timeout_seconds: float = 25.0,
        expected_project_id: str | None = None,
        uv_apply_source_id: str | None = None,
        uv_apply_candidate_id: str | None = None,
        resource_barrier: bool = False,
    ) -> dict[str, Any]:
        """Submit once and return a redacted receipt on a proven response.

        ``api_origin`` defaults to the CN API origin; pass the global origin or
        an explicit origin for another region.  ``expected_project_id`` is
        required for postprocess responses that return only an operator id; it
        is also used to reject any conflicting project id in the response.
        """
        endpoint_path = _endpoint_path(endpoint)
        origin = _origin_key(
            DEFAULT_API_ORIGIN_CN if api_origin is None else api_origin
        )
        if not isinstance(submit_target, str) or not submit_target.strip():
            raise SubmissionConfigError("submit_target_missing")
        if (
            isinstance(expected_count, bool)
            or not isinstance(expected_count, int)
            or expected_count < 1
        ):
            raise SubmissionConfigError("expected_count_invalid")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or float(timeout_seconds) < 0
        ):
            raise SubmissionConfigError("timeout_seconds_invalid")
        timeout = float(timeout_seconds)
        expected_project: str | None = None
        if expected_project_id is not None:
            expected_project = _safe_id(expected_project_id)
            if expected_project is None:
                raise SubmissionConfigError("expected_project_id_invalid")
        uv_apply=uv_apply_source_id is not None or uv_apply_candidate_id is not None
        if uv_apply and (endpoint_path!='/v2/studio/operation/uv_edit/apply' or expected_count!=1 or expected_project is None or not _safe_id(uv_apply_source_id) or not _safe_id(uv_apply_candidate_id) or uv_apply_source_id==uv_apply_candidate_id):
            raise SubmissionConfigError('uv_apply_identity_invalid')
        if endpoint_path=='/v2/studio/operation/uv_edit/apply' and not uv_apply:
            raise SubmissionConfigError('uv_apply_requires_exact_source_and_candidate')

        # 1. Baseline only.  Any failure here is provably before intent/click.
        try:
            baseline_snapshot = parse_network_snapshot(
                self._decode_output(self._call("network"))
            )
        except Exception:
            raise SubmissionNotAttempted(
                "baseline_network_failed",
                endpoint=endpoint_path,
                expected_count=expected_count,
            ) from None

        baseline_entries = baseline_snapshot["entries"]
        baseline_floor = _baseline_floor(baseline_snapshot)
        if resource_barrier and baseline_floor is None:
            raise SubmissionNotAttempted('resource_barrier_requires_baseline_time',endpoint=endpoint_path,expected_count=expected_count)
        baseline_keys: set[str] = set()
        for entry in baseline_entries:
            key = network_entry_key(entry)
            if key is not None:
                baseline_keys.add(key)

        # 2. Intent must be durable before the only click is attempted.
        intent_evidence = {
            "endpoint": endpoint_path,
            "expected_count": expected_count,
        }
        self._commit_once(
            "submitting",
            intent_evidence,
            click_attempted=False,
        )

        # 3. Exactly one structured click.  No eval, fetch, Pinia, or retry.
        try:
            click_raw = self._call("click", submit_target)
            click_envelope = self._decode_output(click_raw)
        except Exception as exc:
            self._halt_unknown(
                "click_exception",
                endpoint=endpoint_path,
                expected_count=expected_count,
                code=getattr(exc,'code',None),
            )

        if not isinstance(click_envelope, Mapping):
            self._halt_unknown(
                "click_envelope_invalid",
                endpoint=endpoint_path,
                expected_count=expected_count,
            )
        click_error = click_envelope.get("error")
        if click_error:
            click_code = (
                _safe_code(click_error.get("code"))
                if isinstance(click_error, Mapping)
                else None
            )
            self._halt_unknown(
                "click_error",
                endpoint=endpoint_path,
                expected_count=expected_count,
                code=click_code,
            )
        if click_envelope.get("clicked") is not True:
            self._halt_unknown(
                "click_not_confirmed",
                endpoint=endpoint_path,
                expected_count=expected_count,
            )
        matches_n = click_envelope.get("matches_n")
        if matches_n is not None and _coerce_int(matches_n) != 1:
            self._halt_unknown(
                "click_ambiguous",
                endpoint=endpoint_path,
                expected_count=expected_count,
            )

        # 4. Do not drain the capture ring while the response is in flight.
        # Some capture implementations retain pending array indices across reads.
        # ResourceTiming is only a readiness barrier; HTTP receipt checks below
        # remain authoritative and no request is replayed.
        if resource_barrier:
            opts=json.dumps({'origin':DEFAULT_API_ORIGIN_CN if api_origin is None else api_origin,'path':endpoint_path,'floor':baseline_floor*1000})
            js=r'''(opts=>{const origin=new URL(opts.origin).origin;const matches=performance.getEntriesByType('resource').filter(e=>{const u=new URL(e.name,location.href);return u.origin===origin&&u.pathname===opts.path&&performance.timeOrigin+e.startTime>=opts.floor&&e.responseEnd>e.startTime;});return {complete:matches.length===1,count:matches.length};})(OPTS)'''.replace('OPTS',opts)
            resource_deadline=self._clock()+timeout
            while True:
                try:ready=self._decode_output(self._call('eval',js))
                except Exception:self._halt_unknown('resource_barrier_read_failed',endpoint=endpoint_path,expected_count=expected_count)
                if not isinstance(ready,Mapping) or type(ready.get('count')) is not int:
                    self._halt_unknown('resource_barrier_invalid',endpoint=endpoint_path,expected_count=expected_count)
                if ready['count']>1:self._halt_unknown('duplicate_resource_responses',endpoint=endpoint_path,expected_count=expected_count)
                if ready.get('complete') is True and ready['count']==1:break
                if self._clock()>=resource_deadline:self._halt_unknown('resource_barrier_timeout',endpoint=endpoint_path,expected_count=expected_count)
                self._sleep(self._poll_interval)
            self._sleep(.25)

        # Poll only for a *new* key whose URL/method match this endpoint.
        deadline = self._clock() + timeout
        target_key: str | None = None
        target_entry: Mapping[str, Any] | None = None
        while target_key is None:
            try:
                poll_snapshot = parse_network_snapshot(
                    self._decode_output(self._call("network"))
                )
            except Exception:
                self._halt_unknown(
                    "network_exception_after_click",
                    endpoint=endpoint_path,
                    expected_count=expected_count,
                )

            entries = poll_snapshot["entries"]
            targets: dict[str, Mapping[str, Any]] = {}
            for entry in entries:
                key = network_entry_key(entry)
                if key is None or key in baseline_keys:
                    continue
                if not _entry_matches(entry, origin, endpoint_path):
                    continue
                if baseline_floor is not None:
                    raw_timestamp = entry.get("timestamp")
                    entry_timestamp = parse_timestamp(raw_timestamp)
                    if entry_timestamp is None:
                        reason = (
                            "timestamp_missing"
                            if raw_timestamp in (None, "")
                            else "timestamp_invalid"
                        )
                        self._halt_unknown(
                            reason,
                            endpoint=endpoint_path,
                            expected_count=expected_count,
                            key=key,
                        )
                    if entry_timestamp < baseline_floor:
                        # A matching path/method with an old timestamp is not
                        # proof that this click produced it; keep polling.
                        continue
                targets[key] = entry

            if len(targets) > 1:
                self._halt_unknown(
                    "duplicate_target_responses",
                    endpoint=endpoint_path,
                    expected_count=expected_count,
                    extra={"network_keys": list(targets)},
                )
            if len(targets) == 1:
                target_key, target_entry = next(iter(targets.items()))
                break

            if resource_barrier:
                self._halt_unknown('resource_complete_but_response_missing',endpoint=endpoint_path,expected_count=expected_count)

            remaining = deadline - self._clock()
            if remaining <= 0:
                self._halt_unknown(
                    "no_target_response",
                    endpoint=endpoint_path,
                    expected_count=expected_count,
                )
            try:
                self._sleep(min(self._poll_interval, remaining))
            except Exception:
                self._halt_unknown(
                    "poll_sleep_failed",
                    endpoint=endpoint_path,
                    expected_count=expected_count,
                )

        # 5. One detail read by the exact new key.
        try:
            detail = self._decode_output(
                self._call("network", "--detail", target_key)
            )
        except Exception:
            self._halt_unknown(
                "detail_transport_failed",
                endpoint=endpoint_path,
                expected_count=expected_count,
                key=target_key,
            )
        if not isinstance(detail, Mapping):
            self._halt_unknown(
                "detail_envelope_invalid",
                endpoint=endpoint_path,
                expected_count=expected_count,
                key=target_key,
            )

        detail_key = detail.get("key")
        if detail_key is not None and detail_key != target_key:
            self._halt_unknown(
                "detail_key_mismatch",
                endpoint=endpoint_path,
                expected_count=expected_count,
                key=target_key,
            )

        truncated = detail.get("body_truncated")
        if truncated is True or (
            truncated is None and bool(detail.get("body_truncation_reason"))
        ):
            self._halt_unknown(
                "response_truncated",
                endpoint=endpoint_path,
                expected_count=expected_count,
                key=target_key,
                http_status=detail.get("status") or target_entry.get("status"),
            )

        detail_url = detail.get("url")
        if isinstance(detail_url, str):
            parsed_detail = _url_origin_and_path(detail_url)
            if (
                parsed_detail is None
                or parsed_detail[0] != origin
                or parsed_detail[1] != endpoint_path
            ):
                self._halt_unknown(
                    "detail_url_mismatch",
                    endpoint=endpoint_path,
                    expected_count=expected_count,
                    key=target_key,
                    http_status=detail.get("status") or target_entry.get("status"),
                )
        detail_method = detail.get("method")
        if detail_method is not None and (
            not isinstance(detail_method, str)
            or detail_method.upper() != "POST"
        ):
            self._halt_unknown(
                "detail_method_mismatch",
                endpoint=endpoint_path,
                expected_count=expected_count,
                key=target_key,
                http_status=detail.get("status") or target_entry.get("status"),
            )

        status = _coerce_int(detail.get("status"))
        list_status = _coerce_int(target_entry.get("status"))
        if list_status is not None and not 200 <= list_status <= 299:
            self._halt_unknown(
                "http_error",
                endpoint=endpoint_path,
                expected_count=expected_count,
                key=target_key,
                http_status=list_status,
            )
        if status is None:
            status = list_status
        if status is None:
            self._halt_unknown(
                "status_missing",
                endpoint=endpoint_path,
                expected_count=expected_count,
                key=target_key,
            )
        if not 200 <= status <= 299:
            self._halt_unknown(
                "http_error",
                endpoint=endpoint_path,
                expected_count=expected_count,
                key=target_key,
                http_status=status,
            )

        body = detail.get("body")
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except Exception:
                self._halt_unknown(
                    "response_body_invalid_json",
                    endpoint=endpoint_path,
                    expected_count=expected_count,
                    key=target_key,
                    http_status=status,
                )
        if not isinstance(body, Mapping):
            self._halt_unknown(
                "response_body_not_object",
                endpoint=endpoint_path,
                expected_count=expected_count,
                key=target_key,
                http_status=status,
            )

        try:
            if uv_apply:
                data=body.get('data')
                if type(body.get('code')) is not int or body.get('code')!=0 or not isinstance(data,Mapping) or data.get('project_id')!=expected_project or data.get('previous_operator_id')!=uv_apply_source_id or data.get('current_operator_id')!=uv_apply_candidate_id:
                    raise ReceiptError('uv_apply_identity_mismatch')
                # UV apply activates an existing candidate rather than creating
                # another operator. Preserve its strict three-ID contract.
                body={'code':0,'data':{'project_id':expected_project,'operator_id':uv_apply_candidate_id}}
            parsed = parse_receipt(
                body,
                expected_count,
                expected_project_id=expected_project,
            )
        except ReceiptError as exc:
            exc.details.update(_partial_receipt_ids(body))
            self._halt_unknown(
                exc.reason,
                endpoint=endpoint_path,
                expected_count=expected_count,
                key=target_key,
                http_status=status,
                code=exc.details.get("code"),
                extra=exc.details,
            )
        except Exception:
            self._halt_unknown(
                "receipt_validation_failed",
                endpoint=endpoint_path,
                expected_count=expected_count,
                key=target_key,
                http_status=status,
            )

        receipt: dict[str, Any] = {
            "status": "submitted",
            "endpoint": endpoint_path,
            "network_key": _redacted_key(target_key),
            "http_status": status,
            "code": 0,
            "count": parsed["count"],
            "operator_ids": parsed["operator_ids"],
            "project_ids": parsed["project_ids"],
            "items": parsed["items"],
        }
        if parsed["task_ids"]:
            receipt["task_ids"] = parsed["task_ids"]
        if parsed["count"] == 1:
            only = parsed["items"][0]
            receipt["operator_id"] = only["operator_id"]
            receipt["project_id"] = only["project_id"]
            if "task_id" in only:
                receipt["task_id"] = only["task_id"]

        # A verified receipt is committed once.  A ledger failure is surfaced
        # with the receipt attached; it is never converted into a second click.
        self._commit_once("submitted", receipt, click_attempted=True)
        return {"ok": True, **receipt}
