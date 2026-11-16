#!/usr/bin/env python3
"""Mock-only acceptance tests for tripo_submit.py.

No browser, network, OpenCLI, or ledger process is touched.  The harness
emulates the parent's already-pinned call/decode/commit callbacks and a fake
monotonic clock so timeout paths stay fast and deterministic.
"""
from __future__ import annotations

import copy
import json
import unittest
from typing import Any

from tripo_submit import (
    ALLOWED_UNKNOWN_EVIDENCE_KEYS,
    DEFAULT_API_ORIGIN_CN,
    DEFAULT_API_ORIGIN_GLOBAL,
    NetworkEnvelopeError,
    ReceiptError,
    SingleSubmission,
    SubmissionCommitError,
    SubmissionConfigError,
    SubmissionNotAttempted,
    SubmissionUnknown,
    match_network_entry,
    parse_network_entries,
    parse_network_snapshot,
    parse_receipt,
    parse_timestamp,
)


API_ORIGIN = "https://studio.example.test"
ENDPOINT = "/v2/studio/operation/generate_model"
TARGET = "[data-testid='generate-model']"
SECRET = "QUERY_TOKEN_MUST_NOT_LEAK"
DETAIL_SECRET = "DETAIL_HINT_MUST_NOT_LEAK"


def net_entry(
    key: str,
    *,
    path: str = ENDPOINT,
    method: str = "POST",
    origin: str = API_ORIGIN,
    status: int = 200,
    timestamp: Any = None,
) -> dict[str, Any]:
    entry = {
        "key": key,
        "method": method,
        "status": status,
        "url": f"{origin}{path}?token={SECRET}",
    }
    if timestamp is not None:
        entry["timestamp"] = timestamp
    return entry


def detail_envelope(
    key: str,
    body: Any,
    *,
    status: int = 200,
    url: str | None = None,
    method: str = "POST",
    **extra: Any,
) -> dict[str, Any]:
    envelope = {
        "key": key,
        "url": url or f"{API_ORIGIN}{ENDPOINT}?token={SECRET}",
        "method": method,
        "status": status,
        "body": body,
    }
    envelope.update(extra)
    return envelope


def success_body(count: int = 1) -> dict[str, Any]:
    if count == 1:
        data: Any = {
            "operator_id": "op-1",
            "project_id": "proj-1",
            "credits": 50,
            "price": 50,
            "message": SECRET,
            "signed_url": f"https://signed.example/model?X-Amz-Signature={SECRET}",
            "download_url": f"https://signed.example/download?token={SECRET}",
        }
    else:
        data = {
            "project_id": "proj-shared",
            "tasks": [
                {"operator_id": f"op-{index}", "message": SECRET}
                for index in range(1, count + 1)
            ],
        }
    return {"code": 0, "data": data, "message": SECRET}


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += float(seconds)


class Harness:
    """Emulates pinned OpenCLI callbacks with JSON CLI text."""

    def __init__(
        self,
        *,
        baseline: Any,
        click: Any = None,
        lists: list[Any] | tuple[Any, ...] = (),
        details: dict[str, Any] | None = None,
        baseline_exc: Exception | None = None,
        click_exc: Exception | None = None,
        list_exc: Exception | None = None,
        detail_exc: Exception | None = None,
        commit_raise: set[str] | None = None,
        commit_reject: set[str] | None = None,
    ) -> None:
        self.baseline = baseline
        self.click = click if click is not None else {"clicked": True, "matches_n": 1}
        self.lists = list(lists)
        self.details = dict(details or {})
        self.baseline_exc = baseline_exc
        self.click_exc = click_exc
        self.list_exc = list_exc
        self.detail_exc = detail_exc
        self.commit_raise = set(commit_raise or ())
        self.commit_reject = set(commit_reject or ())
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.list_calls = 0
        self.click_calls = 0
        self.detail_calls: list[str] = []
        self.commits: list[tuple[str, dict[str, Any]]] = []
        self.clock = FakeClock()

    def call(self, operation: str, *args: Any) -> str:
        self.calls.append((operation, args))
        if operation == "click":
            self.click_calls += 1
            if self.click_exc is not None:
                raise self.click_exc
            return json.dumps(self.click)
        if operation != "network":
            raise AssertionError(f"unexpected operation: {operation}")

        if args:
            if len(args) != 2 or args[0] != "--detail":
                raise AssertionError(f"unexpected detail args: {args!r}")
            key = args[1]
            self.detail_calls.append(key)
            if self.detail_exc is not None:
                raise self.detail_exc
            if key not in self.details:
                raise KeyError(key)
            return json.dumps(self.details[key])

        self.list_calls += 1
        if self.list_calls == 1:
            if self.baseline_exc is not None:
                raise self.baseline_exc
            return json.dumps(self.baseline)
        if self.list_exc is not None:
            raise self.list_exc
        index = self.list_calls - 2
        if not self.lists:
            payload: Any = []
        else:
            payload = self.lists[min(index, len(self.lists) - 1)]
        return json.dumps(payload)

    def commit(self, status: str, evidence: dict[str, Any]) -> Any:
        self.commits.append((status, copy.deepcopy(evidence)))
        if status in self.commit_raise:
            raise RuntimeError(f"ledger failed at {API_ORIGIN}/x?token={SECRET}")
        if status in self.commit_reject:
            return {"ok": False, "error": f"rejected {SECRET}"}
        return {"ok": True}

    def run(
        self,
        *,
        expected_count: int = 1,
        timeout_seconds: float = 2.0,
        api_origin: str | None = API_ORIGIN,
        expected_project_id: str | None = None,
    ) -> dict[str, Any]:
        submission = SingleSubmission(
            self.call,
            json.loads,
            self.commit,
            sleep=self.clock.sleep,
            clock=self.clock,
            poll_interval=0.5,
        )
        # Use the documented positional order to prove integration compatibility.
        return submission.run(
            ENDPOINT,
            TARGET,
            expected_count,
            api_origin,
            timeout_seconds,
            expected_project_id,
        )


class SubmitTestCase(unittest.TestCase):
    def assert_no_secret(self, value: Any) -> None:
        blob = json.dumps(value, ensure_ascii=False, default=str)
        self.assertNotIn(SECRET, blob)

    def assert_safe_evidence(self, evidence: dict[str, Any]) -> None:
        self.assertLessEqual(set(evidence), ALLOWED_UNKNOWN_EVIDENCE_KEYS)
        self.assert_no_secret(evidence)

    def unknown_from(self, harness: Harness, **run_kwargs: Any) -> SubmissionUnknown:
        with self.assertRaises(SubmissionUnknown) as caught:
            harness.run(**run_kwargs)
        self.assert_safe_evidence(caught.exception.evidence)
        self.assert_no_secret(str(caught.exception))
        return caught.exception


class PureFunctionTests(SubmitTestCase):
    def test_network_envelope_accepts_list_entries_and_requests(self) -> None:
        entries = [net_entry("k1")]
        self.assertEqual(parse_network_entries(entries), entries)
        self.assertEqual(parse_network_entries({"entries": entries}), entries)
        self.assertEqual(parse_network_entries({"requests": entries}), entries)
        with self.assertRaises(NetworkEnvelopeError):
            parse_network_entries({"data": entries})
        with self.assertRaises(NetworkEnvelopeError):
            parse_network_entries({"entries": "not-a-list"})

    def test_parse_network_snapshot_actual_envelope_and_bad_captured_at(self) -> None:
        payload = {
            "session": "session-id",
            "captured_at": "2026-11-15T12:00:00Z",
            "count": 1,
            "entries": [
                net_entry(
                    f"{ENDPOINT}#1",
                    timestamp="2026-11-15T11:59:00Z",
                )
            ],
            "detail_hint": f"https://signed.example/detail?token={DETAIL_SECRET}",
        }
        snapshot = parse_network_snapshot(payload)
        self.assertEqual(len(snapshot["entries"]), 1)
        self.assertEqual(set(snapshot), {"entries", "captured_at"})
        self.assertIsNotNone(snapshot["captured_at"])
        self.assertNotIn("session", snapshot)
        self.assertNotIn("detail_hint", snapshot)
        self.assertNotIn(DETAIL_SECRET, json.dumps(snapshot))
        self.assertIsNone(parse_network_snapshot([net_entry("k")])["captured_at"])
        with self.assertRaises(NetworkEnvelopeError):
            parse_network_snapshot({**payload, "captured_at": "not-a-time"})

    def test_parse_timestamp_supports_iso_and_epoch_units(self) -> None:
        iso = parse_timestamp("2026-11-15T12:00:00Z")
        self.assertIsNotNone(iso)
        self.assertAlmostEqual(iso, parse_timestamp("2026-11-15T13:00:00+01:00"))
        self.assertAlmostEqual(iso, parse_timestamp("2026-11-15T12:00:00"))
        self.assertAlmostEqual(iso, parse_timestamp(int(iso * 1000)))
        self.assertAlmostEqual(iso, parse_timestamp(int(iso * 1_000_000)))
        self.assertAlmostEqual(iso, parse_timestamp(int(iso * 1_000_000_000)))
        self.assertIsNone(parse_timestamp("not-a-time"))
        self.assertIsNone(parse_timestamp(None))

    def test_strict_match_requires_post_origin_and_exact_path(self) -> None:
        matching = net_entry("new")
        self.assertTrue(match_network_entry(matching, API_ORIGIN, ENDPOINT))
        self.assertFalse(
            match_network_entry(
                net_entry("prefix", path="/v2/studio/operation/ai_segmentation"),
                API_ORIGIN,
                ENDPOINT,
            )
        )
        self.assertFalse(
            match_network_entry(
                net_entry("suffix", path=ENDPOINT + "_extra"),
                API_ORIGIN,
                ENDPOINT,
            )
        )
        self.assertFalse(
            match_network_entry(
                net_entry("origin", origin="https://other.example.test"),
                API_ORIGIN,
                ENDPOINT,
            )
        )
        self.assertFalse(
            match_network_entry(net_entry("get", method="GET"), API_ORIGIN, ENDPOINT)
        )
        self.assertFalse(match_network_entry("not-an-entry", API_ORIGIN, ENDPOINT))

    def test_parse_receipt_single_object_drops_price_and_message(self) -> None:
        parsed = parse_receipt(
            {
                "code": 0,
                "data": {
                    "operator_id": "op-1",
                    "project_id": "proj-1",
                    "credits": 50,
                    "actual_credits": 50,
                    "message": SECRET,
                },
                "message": SECRET,
            },
            expected_count=1,
        )
        self.assertEqual(parsed["count"], 1)
        self.assertEqual(parsed["operator_ids"], ["op-1"])
        self.assertEqual(parsed["project_ids"], ["proj-1"])
        self.assertNotIn("credits", parsed)
        self.assertNotIn("actual_credits", parsed)
        self.assertNotIn("signed_url", parsed)
        self.assertNotIn("download_url", parsed)
        self.assert_no_secret(parsed)

    def test_parse_receipt_supports_array_and_tasks_with_inherited_project(self) -> None:
        array = parse_receipt(
            {
                "code": 0,
                "data": [
                    {"task_id": "task-a", "project_id": "proj-a"},
                    {"operator_id": "op-b", "project_id": "proj-b"},
                ],
            },
            expected_count=2,
        )
        self.assertEqual(array["operator_ids"], ["task-a", "op-b"])
        self.assertEqual(array["project_ids"], ["proj-a", "proj-b"])

        tasks = parse_receipt(
            {
                "code": 0,
                "data": {
                    "project_id": "proj-shared",
                    "tasks": [
                        {"operator_id": "op-1"},
                        {"operator_id": "op-2"},
                    ],
                },
            },
            expected_count=2,
        )
        self.assertEqual(tasks["operator_ids"], ["op-1", "op-2"])
        self.assertEqual(tasks["project_ids"], ["proj-shared", "proj-shared"])

    def test_parse_receipt_never_recurses_for_ids(self) -> None:
        with self.assertRaises(ReceiptError) as caught:
            parse_receipt(
                {
                    "code": 0,
                    "operator_id": "top-level-ignored",
                    "project_id": "top-level-ignored",
                    "data": {
                        "meta": {"operator_id": "nested", "project_id": "nested"}
                    },
                },
                expected_count=1,
            )
        self.assertEqual(caught.exception.reason, "ids_incomplete")

    def test_parse_receipt_rejects_nonzero_incomplete_and_count_mismatch(self) -> None:
        with self.assertRaises(ReceiptError) as caught:
            parse_receipt(
                {
                    "code": 1001,
                    "data": {"operator_id": "op", "project_id": "proj"},
                },
                expected_count=1,
            )
        self.assertEqual(caught.exception.reason, "non_zero_code")
        self.assertEqual(caught.exception.details["code"], 1001)

        with self.assertRaises(ReceiptError) as caught:
            parse_receipt(
                {"code": 0, "data": {"operator_id": "op"}},
                expected_count=1,
            )
        self.assertEqual(caught.exception.reason, "ids_incomplete")
        self.assertEqual(caught.exception.details["field"], "project_id")

        with self.assertRaises(ReceiptError) as caught:
            parse_receipt(
                {
                    "code": 0,
                    "data": {"operator_id": "op", "project_id": "proj"},
                },
                expected_count=2,
            )
        self.assertEqual(caught.exception.reason, "count_mismatch")
        self.assertEqual(caught.exception.details["count"], 1)
        self.assertEqual(caught.exception.details["expected_count"], 2)

    def test_parse_receipt_rejects_duplicate_operator_ids(self) -> None:
        with self.assertRaises(ReceiptError) as caught:
            parse_receipt(
                {
                    "code": 0,
                    "data": [
                        {"operator_id": "op-duplicate", "project_id": "proj-a"},
                        {"operator_id": "op-duplicate", "project_id": "proj-b"},
                    ],
                },
                expected_count=2,
            )
        self.assertEqual(caught.exception.reason, "duplicate_operator_id")
        self.assertEqual(caught.exception.details["index"], 1)

    def test_parse_receipt_expected_project_fill_mismatch_and_url_ids(self) -> None:
        fixed = "00000001-0000-4000-8000-000000000001"
        filled = parse_receipt(
            {"code": 0, "data": {"operator_id": "op-post"}},
            expected_count=1,
            expected_project_id=fixed,
        )
        self.assertEqual(filled["operator_ids"], ["op-post"])
        self.assertEqual(filled["project_ids"], [fixed])

        with self.assertRaises(ReceiptError) as caught:
            parse_receipt(
                {
                    "code": 0,
                    "data": {"operator_id": "op-post", "project_id": "other-project"},
                },
                expected_count=1,
                expected_project_id=fixed,
            )
        self.assertEqual(caught.exception.reason, "project_id_mismatch")

        with self.assertRaises(ReceiptError) as caught:
            parse_receipt(
                {
                    "code": 0,
                    "data": {
                        "project_id": "container-project",
                        "tasks": [
                            {"operator_id": "op-post", "project_id": fixed}
                        ],
                    },
                },
                expected_count=1,
                expected_project_id=fixed,
            )
        self.assertEqual(caught.exception.reason, "project_id_mismatch")
        self.assertNotIn("container-project", str(caught.exception))

        with self.assertRaises(ReceiptError):
            parse_receipt(
                {
                    "code": 0,
                    "data": {
                        "operator_id": "op-post",
                        "project_id": "https://signed.example/object",
                    },
                },
                expected_count=1,
            )
        with self.assertRaises(ValueError):
            parse_receipt(
                {"code": 0, "data": {"operator_id": "op-post"}},
                expected_count=1,
                expected_project_id="https://signed.example/object",
            )


class RunSuccessTests(SubmitTestCase):
    def test_default_api_origins_and_cn_default(self) -> None:
        self.assertEqual(DEFAULT_API_ORIGIN_CN, "https://api-cn.tripo3d.com")
        self.assertEqual(DEFAULT_API_ORIGIN_GLOBAL, "https://api.tripo3d.ai")
        key = f"{ENDPOINT}#2"
        harness = Harness(
            baseline={"entries": []},
            lists=[
                {
                    "entries": [
                        net_entry(key, origin=DEFAULT_API_ORIGIN_CN),
                    ]
                }
            ],
            details={
                key: detail_envelope(
                    key,
                    success_body(),
                    url=f"{DEFAULT_API_ORIGIN_CN}{ENDPOINT}?token={SECRET}",
                )
            },
        )
        result = harness.run(api_origin=None)
        self.assertEqual(result["network_key"], key)
        self.assertEqual(result["operator_ids"], ["op-1"])
        self.assert_no_secret({"result": result, "commits": harness.commits})

        global_key = f"{ENDPOINT}#2"
        global_harness = Harness(
            baseline={"entries": []},
            lists=[
                {
                    "entries": [
                        net_entry(global_key, origin=DEFAULT_API_ORIGIN_GLOBAL),
                    ]
                }
            ],
            details={
                global_key: detail_envelope(
                    global_key,
                    success_body(),
                    url=f"{DEFAULT_API_ORIGIN_GLOBAL}{ENDPOINT}?token={SECRET}",
                )
            },
        )
        global_result = global_harness.run(api_origin=DEFAULT_API_ORIGIN_GLOBAL)
        self.assertEqual(global_result["operator_ids"], ["op-1"])
        self.assert_no_secret(
            {"result": global_result, "commits": global_harness.commits}
        )

    def test_actual_envelope_timestamp_and_sequence_key(self) -> None:
        key = f"{ENDPOINT}#2"
        baseline = {
            "session": "session-id",
            "captured_at": "2026-11-15T12:00:00Z",
            "count": 1,
            "entries": [
                net_entry(
                    f"{ENDPOINT}#1",
                    timestamp="2026-11-15T11:59:00Z",
                )
            ],
            "detail_hint": f"https://signed.example?token={SECRET}",
        }
        poll = {
            "session": "session-id",
            "captured_at": "2026-11-15T12:00:01Z",
            "count": 2,
            "entries": [
                net_entry(
                    f"{ENDPOINT}#1",
                    timestamp="2026-11-15T11:59:00Z",
                ),
                net_entry(key, timestamp="2026-11-15T12:00:00Z"),
            ],
            "detail_hint": f"https://signed.example?token={SECRET}",
        }
        harness = Harness(
            baseline=baseline,
            lists=[poll],
            details={key: detail_envelope(key, success_body())},
        )
        result = harness.run()

        self.assertEqual(result["network_key"], key)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["operator_ids"], ["op-1"])
        self.assertEqual(harness.detail_calls, [key])
        self.assert_no_secret({"result": result, "commits": harness.commits})

    def test_stale_target_then_fresh_target_succeeds(self) -> None:
        stale_key = f"{ENDPOINT}#2"
        fresh_key = f"{ENDPOINT}#3"
        stale_entry = net_entry(
            stale_key,
            timestamp="2026-11-15T11:59:59Z",
        )
        fresh_entry = net_entry(
            fresh_key,
            timestamp="2026-11-15T12:00:01Z",
        )
        harness = Harness(
            baseline={
                "captured_at": "2026-11-15T12:00:00Z",
                "entries": [],
            },
            lists=[
                {"entries": [stale_entry]},
                {"entries": [stale_entry, fresh_entry]},
            ],
            details={fresh_key: detail_envelope(fresh_key, success_body())},
        )
        result = harness.run(timeout_seconds=5.0)

        self.assertEqual(result["network_key"], fresh_key)
        self.assertEqual(harness.detail_calls, [fresh_key])
        self.assertEqual(harness.click_calls, 1)

    def test_postprocess_operator_only_uses_expected_project(self) -> None:
        expected_project = "00000002-0000-4000-8000-000000000002"
        body = {
            "code": 0,
            "data": {
                "operator_id": "op-postprocess",
                "message": SECRET,
                "model_url": f"https://signed.example/model?X-Amz-Signature={SECRET}",
            },
        }
        harness = Harness(
            baseline={"entries": []},
            lists=[{"entries": [net_entry("target-key")]}],
            details={"target-key": detail_envelope("target-key", body)},
        )
        result = harness.run(expected_project_id=expected_project)

        self.assertEqual(result["operator_ids"], ["op-postprocess"])
        self.assertEqual(result["project_ids"], [expected_project])
        self.assertEqual(result["operator_id"], "op-postprocess")
        self.assertEqual(result["project_id"], expected_project)
        self.assertEqual(len(result["operator_ids"]), result["count"])
        self.assertEqual(len(result["project_ids"]), result["count"])
        self.assertEqual([status for status, _ in harness.commits], ["submitting", "submitted"])
        self.assert_no_secret({"result": result, "commits": harness.commits})

    def test_postprocess_tasks_fill_expected_project_for_each_operator(self) -> None:
        expected_project = "00000003-0000-4000-8000-000000000003"
        body = {
            "code": 0,
            "data": {
                "tasks": [
                    {"operator_id": "op-a"},
                    {"operator_id": "op-b"},
                ]
            },
        }
        harness = Harness(
            baseline={"entries": []},
            lists=[{"entries": [net_entry("target-key")]}],
            details={"target-key": detail_envelope("target-key", body)},
        )
        result = harness.run(
            expected_count=2,
            expected_project_id=expected_project,
        )

        self.assertEqual(result["operator_ids"], ["op-a", "op-b"])
        self.assertEqual(result["project_ids"], [expected_project, expected_project])
        self.assertEqual(result["items"][0]["project_id"], expected_project)
        self.assertEqual(result["items"][1]["project_id"], expected_project)
        self.assertEqual(len(result["operator_ids"]), result["count"])
        self.assertEqual(len(result["project_ids"]), result["count"])

    def test_success_ignores_baseline_old_key_and_redacts_receipt(self) -> None:
        harness = Harness(
            baseline={"entries": [net_entry("old-key")]},
            lists=[{"entries": [net_entry("old-key"), net_entry("new-key")]}],
            details={"new-key": detail_envelope("new-key", success_body())},
        )
        result = harness.run()

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "submitted")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["operator_id"], "op-1")
        self.assertEqual(result["project_id"], "proj-1")
        self.assertEqual(result["operator_ids"], ["op-1"])
        self.assertEqual(result["project_ids"], ["proj-1"])
        self.assertEqual(result["network_key"], "new-key")
        self.assertEqual(result["http_status"], 200)
        self.assertNotIn("credits", result)
        self.assertNotIn("actual_credits", result)

        self.assertEqual(harness.click_calls, 1)
        self.assertEqual(harness.detail_calls, ["new-key"])
        self.assertEqual([status for status, _ in harness.commits], ["submitting", "submitted"])
        self.assertEqual(
            {operation for operation, _ in harness.calls},
            {"network", "click"},
        )
        self.assert_no_secret({"result": result, "commits": harness.commits})

    def test_success_uses_requests_envelope_and_ignores_non_target_key(self) -> None:
        harness = Harness(
            baseline={"requests": []},
            lists=[
                {
                    "requests": [
                        net_entry(
                            "other-operation",
                            path="/v2/studio/operation/ai_segmentation",
                        )
                    ]
                },
                {"requests": [net_entry("target-key")]},
            ],
            details={"target-key": detail_envelope("target-key", success_body())},
        )
        result = harness.run(timeout_seconds=5.0)

        self.assertEqual(result["operator_ids"], ["op-1"])
        self.assertEqual(result["network_key"], "target-key")
        self.assertEqual(harness.detail_calls, ["target-key"])
        self.assertEqual(harness.click_calls, 1)
        self.assertGreaterEqual(harness.clock.value, 0.5)

    def test_success_multi_ids_from_data_tasks(self) -> None:
        harness = Harness(
            baseline={"entries": []},
            lists=[{"entries": [net_entry("multi-key")]}],
            details={"multi-key": detail_envelope("multi-key", success_body(2))},
        )
        result = harness.run(expected_count=2)

        self.assertEqual(result["count"], 2)
        self.assertEqual(result["operator_ids"], ["op-1", "op-2"])
        self.assertEqual(result["project_ids"], ["proj-shared", "proj-shared"])
        self.assertEqual(
            result["items"],
            [
                {"operator_id": "op-1", "project_id": "proj-shared"},
                {"operator_id": "op-2", "project_id": "proj-shared"},
            ],
        )
        self.assertNotIn("operator_id", result)
        self.assertNotIn("actual_credits", result)
        self.assertEqual([status for status, _ in harness.commits], ["submitting", "submitted"])
        self.assert_no_secret({"result": result, "commits": harness.commits})

    def test_success_accepts_json_string_body(self) -> None:
        harness = Harness(
            baseline={"entries": []},
            lists=[{"entries": [net_entry("target-key")]}],
            details={
                "target-key": detail_envelope(
                    "target-key",
                    json.dumps(success_body()),
                )
            },
        )
        result = harness.run()

        self.assertEqual(result["operator_id"], "op-1")
        self.assertEqual(result["project_id"], "proj-1")
        self.assertEqual([status for status, _ in harness.commits], ["submitting", "submitted"])

    def test_unsafe_network_key_is_hashed_in_receipt(self) -> None:
        raw_key = f"cache?token={SECRET}"
        harness = Harness(
            baseline={"entries": []},
            lists=[{"entries": [net_entry(raw_key)]}],
            details={
                raw_key: detail_envelope(raw_key, success_body()),
            },
        )
        result = harness.run()

        self.assertEqual(harness.detail_calls, [raw_key])
        self.assertTrue(result["network_key"].startswith("sha256:"))
        self.assertNotIn(SECRET, result["network_key"])
        self.assert_no_secret({"result": result, "commits": harness.commits})


class RunUnknownTests(SubmitTestCase):
    def test_expected_project_mismatch_commits_unknown(self) -> None:
        harness = Harness(
            baseline={"entries": []},
            lists=[{"entries": [net_entry("target-key")]}],
            details={
                "target-key": detail_envelope(
                    "target-key",
                    {
                        "code": 0,
                        "data": {
                            "operator_id": "op-1",
                            "project_id": "actual-project",
                        },
                    },
                )
            },
        )
        error = self.unknown_from(
            harness,
            expected_project_id="expected-project",
        )

        self.assertEqual(error.reason, "project_id_mismatch")
        self.assertEqual(error.evidence["field"], "project_id")
        self.assertEqual([status for status, _ in harness.commits], ["submitting", "unknown"])
        self.assertEqual(harness.click_calls, 1)

    def test_task_container_project_mismatch_commits_unknown(self) -> None:
        harness = Harness(
            baseline={"entries": []},
            lists=[{"entries": [net_entry("target-key")]}],
            details={
                "target-key": detail_envelope(
                    "target-key",
                    {
                        "code": 0,
                        "data": {
                            "project_id": "container-project",
                            "tasks": [
                                {
                                    "operator_id": "op-1",
                                    "project_id": "expected-project",
                                }
                            ],
                        },
                    },
                )
            },
        )
        error = self.unknown_from(
            harness,
            expected_project_id="expected-project",
        )

        self.assertEqual(error.reason, "project_id_mismatch")
        self.assertEqual(error.evidence["field"], "project_id")
        self.assertNotIn("container-project", str(error))
        self.assertEqual(harness.click_calls, 1)

    def test_url_like_project_id_is_unknown_and_not_persisted(self) -> None:
        body = {
            "code": 0,
            "data": {
                "operator_id": "op-1",
                "project_id": f"https://signed.example/object?token={SECRET}",
            },
        }
        harness = Harness(
            baseline={"entries": []},
            lists=[{"entries": [net_entry("target-key")]}],
            details={"target-key": detail_envelope("target-key", body)},
        )
        error = self.unknown_from(harness)

        self.assertEqual(error.reason, "ids_incomplete")
        self.assertEqual(error.evidence["field"], "project_id")
        self.assertEqual(harness.click_calls, 1)

    def test_stale_target_timestamp_is_not_accepted(self) -> None:
        key = f"{ENDPOINT}#2"
        harness = Harness(
            baseline={
                "captured_at": "2026-11-15T12:00:00Z",
                "entries": [],
            },
            lists=[
                {
                    "captured_at": "2026-11-15T12:00:01Z",
                    "entries": [
                        net_entry(
                            key,
                            timestamp="2026-11-15T11:59:59Z",
                        )
                    ],
                }
            ],
        )
        error = self.unknown_from(harness, timeout_seconds=1.0)

        self.assertEqual(error.reason, "no_target_response")
        self.assertEqual(harness.detail_calls, [])
        self.assertEqual(harness.click_calls, 1)

    def test_missing_or_invalid_target_timestamp_is_unknown(self) -> None:
        cases = [
            (None, "timestamp_missing"),
            ("not-a-time", "timestamp_invalid"),
        ]
        for timestamp, reason in cases:
            with self.subTest(reason=reason):
                key = f"{ENDPOINT}#2"
                entry = net_entry(key)
                if timestamp is not None:
                    entry["timestamp"] = timestamp
                harness = Harness(
                    baseline={
                        "captured_at": "2026-11-15T12:00:00Z",
                        "entries": [],
                    },
                    lists=[{"entries": [entry]}],
                )
                error = self.unknown_from(harness)
                self.assertEqual(error.reason, reason)
                self.assertEqual(harness.detail_calls, [])
                self.assertEqual(harness.click_calls, 1)

    def test_expected_count_mismatch_commits_unknown(self) -> None:
        harness = Harness(
            baseline={"entries": []},
            lists=[{"entries": [net_entry("target-key")]}],
            details={
                "target-key": detail_envelope("target-key", success_body(1))
            },
        )
        error = self.unknown_from(harness, expected_count=2)

        self.assertEqual(error.reason, "count_mismatch")
        self.assertEqual(error.evidence["count"], 1)
        self.assertEqual(error.evidence["expected_count"], 2)
        self.assertEqual([status for status, _ in harness.commits], ["submitting", "unknown"])
        self.assertEqual(harness.click_calls, 1)

    def test_incomplete_ids_commit_unknown_and_do_not_log_body(self) -> None:
        body = {
            "code": 0,
            "data": {"operator_id": "op-only", "message": SECRET},
            "message": SECRET,
        }
        harness = Harness(
            baseline={"entries": []},
            lists=[{"entries": [net_entry("target-key")]}],
            details={"target-key": detail_envelope("target-key", body)},
        )
        error = self.unknown_from(harness)

        self.assertEqual(error.reason, "ids_incomplete")
        self.assertEqual(error.evidence["field"], "project_id")
        self.assertEqual([status for status, _ in harness.commits], ["submitting", "unknown"])
        self.assertEqual(harness.click_calls, 1)

    def test_nonzero_code_commits_unknown(self) -> None:
        body = {
            "code": 1001,
            "data": {"operator_id": "op", "project_id": "proj"},
            "message": SECRET,
        }
        harness = Harness(
            baseline={"entries": []},
            lists=[{"entries": [net_entry("target-key")]}],
            details={"target-key": detail_envelope("target-key", body)},
        )
        error = self.unknown_from(harness)

        self.assertEqual(error.reason, "non_zero_code")
        self.assertEqual(error.evidence["code"], 1001)
        self.assertEqual([status for status, _ in harness.commits], ["submitting", "unknown"])

    def test_truncated_response_commits_unknown(self) -> None:
        harness = Harness(
            baseline={"entries": []},
            lists=[{"entries": [net_entry("target-key")]}],
            details={
                "target-key": detail_envelope(
                    "target-key",
                    success_body(),
                    body_truncated=True,
                    body_truncation_reason=SECRET,
                )
            },
        )
        error = self.unknown_from(harness)

        self.assertEqual(error.reason, "response_truncated")
        self.assertEqual(harness.detail_calls, ["target-key"])
        self.assertEqual([status for status, _ in harness.commits], ["submitting", "unknown"])

    def test_click_exception_is_unknown_and_never_retried(self) -> None:
        harness = Harness(
            baseline={"entries": []},
            click_exc=RuntimeError(f"click bridge failed at {API_ORIGIN}{ENDPOINT}?token={SECRET}"),
            lists=[{"entries": [net_entry("target-key")]}],
            details={"target-key": detail_envelope("target-key", success_body())},
        )
        error = self.unknown_from(harness)

        self.assertEqual(error.reason, "click_exception")
        self.assertEqual(harness.click_calls, 1)
        self.assertEqual(harness.detail_calls, [])
        self.assertEqual([status for status, _ in harness.commits], ["submitting", "unknown"])

    def test_unconfirmed_or_ambiguous_click_is_unknown(self) -> None:
        cases = [
            ({"clicked": False}, "click_not_confirmed", None),
            ({"clicked": True, "matches_n": 2}, "click_ambiguous", None),
            (
                {"error": {"code": "selector_not_found", "message": SECRET}},
                "click_error",
                "selector_not_found",
            ),
        ]
        for click_envelope, reason, code in cases:
            with self.subTest(reason=reason):
                harness = Harness(
                    baseline={"entries": []},
                    click=click_envelope,
                    lists=[{"entries": [net_entry("target-key")]}],
                    details={"target-key": detail_envelope("target-key", success_body())},
                )
                error = self.unknown_from(harness)
                self.assertEqual(error.reason, reason)
                if code is not None:
                    self.assertEqual(error.evidence["code"], code)
                self.assertEqual(harness.click_calls, 1)
                self.assertEqual(harness.detail_calls, [])

    def test_empty_capture_times_out_unknown_with_one_click(self) -> None:
        harness = Harness(
            baseline={"entries": []},
            lists=[{"entries": []}],
        )
        error = self.unknown_from(harness, timeout_seconds=1.0)

        self.assertEqual(error.reason, "no_target_response")
        self.assertEqual(harness.click_calls, 1)
        self.assertGreaterEqual(harness.list_calls, 3)
        self.assertEqual([status for status, _ in harness.commits], ["submitting", "unknown"])

    def test_baseline_old_target_key_is_never_reused(self) -> None:
        harness = Harness(
            baseline={"entries": [net_entry("old-target")]},
            lists=[{"entries": [net_entry("old-target")]}],
        )
        error = self.unknown_from(harness, timeout_seconds=1.0)

        self.assertEqual(error.reason, "no_target_response")
        self.assertEqual(harness.detail_calls, [])
        self.assertEqual(harness.click_calls, 1)

    def test_network_exception_after_click_is_unknown(self) -> None:
        harness = Harness(
            baseline={"entries": []},
            list_exc=RuntimeError(f"network list failed at {API_ORIGIN}/?token={SECRET}"),
        )
        error = self.unknown_from(harness)

        self.assertEqual(error.reason, "network_exception_after_click")
        self.assertEqual(harness.click_calls, 1)
        self.assertEqual(harness.detail_calls, [])
        self.assertEqual([status for status, _ in harness.commits], ["submitting", "unknown"])

    def test_duplicate_target_responses_are_unknown(self) -> None:
        harness = Harness(
            baseline={"entries": []},
            lists=[
                {
                    "entries": [
                        net_entry("target-a"),
                        net_entry("target-b"),
                    ]
                }
            ],
        )
        error = self.unknown_from(harness)

        self.assertEqual(error.reason, "duplicate_target_responses")
        self.assertEqual(set(error.evidence["network_keys"]), {"target-a", "target-b"})
        self.assertEqual(harness.detail_calls, [])
        self.assertEqual(harness.click_calls, 1)

    def test_non_targets_are_not_broadly_matched(self) -> None:
        harness = Harness(
            baseline={"entries": []},
            lists=[
                {
                    "entries": [
                        net_entry("prefix", path="/v2/studio/operation/ai_segmentation"),
                        net_entry("suffix", path=ENDPOINT + "_extra"),
                        net_entry("wrong-origin", origin="https://other.example.test"),
                        net_entry("wrong-method", method="GET"),
                    ]
                }
            ],
        )
        error = self.unknown_from(harness, timeout_seconds=0.0)

        self.assertEqual(error.reason, "no_target_response")
        self.assertEqual(harness.detail_calls, [])
        self.assertEqual(harness.click_calls, 1)

    def test_detail_mismatch_and_http_error_are_unknown(self) -> None:
        cases = [
            (
                detail_envelope("different-key", success_body()),
                "detail_key_mismatch",
            ),
            (
                detail_envelope(
                    "target-key",
                    success_body(),
                    url="https://other.example.test" + ENDPOINT,
                ),
                "detail_url_mismatch",
            ),
            (
                detail_envelope("target-key", success_body(), method="GET"),
                "detail_method_mismatch",
            ),
            (
                detail_envelope("target-key", success_body(), status=500),
                "http_error",
            ),
        ]
        for detail, reason in cases:
            with self.subTest(reason=reason):
                harness = Harness(
                    baseline={"entries": []},
                    lists=[{"entries": [net_entry("target-key")]}],
                    details={"target-key": detail},
                )
                error = self.unknown_from(harness)
                self.assertEqual(error.reason, reason)
                self.assertEqual(harness.click_calls, 1)

    def test_unknown_commit_failure_still_raises_unknown(self) -> None:
        harness = Harness(
            baseline={"entries": []},
            lists=[{"entries": [net_entry("target-key")]}],
            details={
                "target-key": detail_envelope(
                    "target-key",
                    {"code": 0, "data": {"operator_id": "op-only"}},
                )
            },
            commit_raise={"unknown"},
        )
        error = self.unknown_from(harness)

        self.assertEqual(error.reason, "ids_incomplete")
        self.assertEqual(error.commit_error_type, "RuntimeError")
        self.assertEqual(harness.click_calls, 1)
        self.assertEqual([status for status, _ in harness.commits], ["submitting", "unknown"])


class CommitBoundaryTests(SubmitTestCase):
    def test_committing_intent_failure_never_clicks(self) -> None:
        harness = Harness(
            baseline={"entries": []},
            lists=[{"entries": [net_entry("target-key")]}],
            details={"target-key": detail_envelope("target-key", success_body())},
            commit_raise={"submitting"},
        )
        with self.assertRaises(SubmissionCommitError) as caught:
            harness.run()

        self.assertEqual(caught.exception.status, "submitting")
        self.assertFalse(caught.exception.click_attempted)
        self.assertEqual(harness.click_calls, 0)
        self.assertEqual(harness.detail_calls, [])
        self.assertEqual([status for status, _ in harness.commits], ["submitting"])
        self.assert_no_secret(str(caught.exception))

    def test_explicit_intent_rejection_never_clicks(self) -> None:
        harness = Harness(
            baseline={"entries": []},
            lists=[{"entries": [net_entry("target-key")]}],
            details={"target-key": detail_envelope("target-key", success_body())},
            commit_reject={"submitting"},
        )
        with self.assertRaises(SubmissionCommitError) as caught:
            harness.run()

        self.assertEqual(caught.exception.cause_type, "rejected")
        self.assertFalse(caught.exception.click_attempted)
        self.assertEqual(harness.click_calls, 0)
        self.assertEqual([status for status, _ in harness.commits], ["submitting"])
        self.assert_no_secret(str(caught.exception))

    def test_submitted_commit_failure_preserves_verified_receipt(self) -> None:
        harness = Harness(
            baseline={"entries": []},
            lists=[{"entries": [net_entry("target-key")]}],
            details={"target-key": detail_envelope("target-key", success_body())},
            commit_raise={"submitted"},
        )
        with self.assertRaises(SubmissionCommitError) as caught:
            harness.run()

        error = caught.exception
        self.assertEqual(error.status, "submitted")
        self.assertTrue(error.click_attempted)
        self.assertEqual(error.evidence["operator_ids"], ["op-1"])
        self.assertEqual(error.evidence["count"], 1)
        self.assertEqual(harness.click_calls, 1)
        self.assertEqual([status for status, _ in harness.commits], ["submitting", "submitted"])
        self.assert_no_secret(str(error))
        self.assert_no_secret(error.evidence)

    def test_preclick_network_failure_is_not_attempted_and_not_aborted(self) -> None:
        harness = Harness(
            baseline={},
            baseline_exc=RuntimeError(f"network down {API_ORIGIN}/?token={SECRET}"),
        )
        with self.assertRaises(SubmissionNotAttempted) as caught:
            harness.run()

        self.assertEqual(caught.exception.reason, "baseline_network_failed")
        self.assertFalse(caught.exception.click_attempted)
        self.assertEqual(harness.commits, [])
        self.assertEqual(harness.click_calls, 0)
        self.assertEqual(harness.list_calls, 1)
        self.assert_no_secret(str(caught.exception))
        self.assert_no_secret(caught.exception.evidence)

    def test_baseline_invalid_captured_at_is_not_attempted(self) -> None:
        harness = Harness(
            baseline={
                "captured_at": "not-a-time",
                "entries": [net_entry("old-key")],
            }
        )
        with self.assertRaises(SubmissionNotAttempted) as caught:
            harness.run()

        self.assertEqual(caught.exception.reason, "baseline_network_failed")
        self.assertFalse(caught.exception.click_attempted)
        self.assertEqual(harness.commits, [])
        self.assertEqual(harness.click_calls, 0)

    def test_config_failures_make_no_browser_calls(self) -> None:
        harness = Harness(baseline={"entries": []})
        submission = SingleSubmission(harness.call, json.loads, harness.commit)

        with self.assertRaises(SubmissionConfigError):
            submission.run(ENDPOINT, TARGET, 1, "https://api.example.test/v2", 1.0)
        with self.assertRaises(SubmissionConfigError):
            submission.run(ENDPOINT + f"?token={SECRET}", TARGET, 1, API_ORIGIN, 1.0)
        with self.assertRaises(SubmissionConfigError):
            submission.run(ENDPOINT, TARGET, 1, "", 1.0)
        with self.assertRaises(SubmissionConfigError):
            submission.run(ENDPOINT, TARGET, 0, API_ORIGIN, 1.0)
        with self.assertRaises(SubmissionConfigError):
            submission.run(
                ENDPOINT,
                TARGET,
                1,
                API_ORIGIN,
                1.0,
                f"https://signed.example/object?token={SECRET}",
            )

        self.assertEqual(harness.calls, [])
        self.assertEqual(harness.commits, [])


if __name__ == "__main__":
    unittest.main()
