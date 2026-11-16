#!/usr/bin/env python3
"""Runtime contract tests for the repaired Tripo ledger (schema v2).

All ledgers are temporary files under this workdir. No real runtime ledger is
read or written. Tests drive the real CLI through subprocesses.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import unittest
import uuid
from pathlib import Path
from typing import Any

WORKDIR = Path(__file__).resolve().parent
SCRIPTS = WORKDIR.parent / "skills/tripo-studio-opencli/scripts"
LEDGER_SCRIPT = SCRIPTS / "tripo_ledger.py"
PYTHON = sys.executable

sys.path.insert(0, str(SCRIPTS))
import tripo_ledger  # noqa: E402


def _child_env() -> dict[str, str]:
    env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    return env


class LedgerCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_path = WORKDIR / f"ledger_test_{uuid.uuid4().hex}"
        self.tmp_path.mkdir()
        self.addCleanup(self._cleanup_tmp)
        self.ledger = self.tmp_path / "ledger.json"
        self.assertTrue(
            self.ledger.resolve().is_relative_to(WORKDIR.resolve()),
            "temporary ledger must stay inside the assigned workdir",
        )

    def _cleanup_tmp(self) -> None:
        shutil.rmtree(self.tmp_path, ignore_errors=True)

    # ---------- helpers ----------
    def cli(self, *args: Any, timeout: float = 30) -> tuple[int, dict[str, Any], str, str]:
        cmd = [
            PYTHON,
            "-B",
            str(LEDGER_SCRIPT),
            "--ledger",
            str(self.ledger),
            *[str(arg) for arg in args],
        ]
        proc = subprocess.run(
            cmd,
            cwd=str(WORKDIR),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=_child_env(),
        )
        stdout = proc.stdout.strip()
        stderr = proc.stderr.strip()
        raw = stdout if proc.returncode == 0 else stderr
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {"_raw_stdout": stdout, "_raw_stderr": stderr}
        return proc.returncode, body, stdout, stderr

    def ok(self, *args: Any, timeout: float = 30) -> dict[str, Any]:
        rc, body, stdout, stderr = self.cli(*args, timeout=timeout)
        self.assertEqual(
            rc,
            0,
            msg=f"expected rc=0, got rc={rc}; stdout={stdout}; stderr={stderr}",
        )
        self.assertTrue(body.get("ok"), body)
        return body

    def fail(self, *args: Any, expect: str | None = None, timeout: float = 30) -> dict[str, Any]:
        rc, body, stdout, stderr = self.cli(*args, timeout=timeout)
        self.assertEqual(
            rc,
            2,
            msg=f"expected rc=2, got rc={rc}; stdout={stdout}; stderr={stderr}",
        )
        self.assertFalse(body.get("ok", False), body)
        text = f"{stdout}\n{stderr}\n{body}"
        if expect is not None:
            self.assertIn(expect, text)
        return body

    def init_args(
        self,
        task_id: str = "t",
        budget: int = 10,
        *,
        max_submissions: int = 2,
        trial_caps: tuple[str, ...] = (),
        download_cap: int = 0,
        force: bool = False,
    ) -> list[Any]:
        args: list[Any] = [
            "init-task",
            "--task-id",
            task_id,
            "--budget-cap",
            budget,
            "--max-submissions",
            max_submissions,
            "--download-cap",
            download_cap,
        ]
        for cap in trial_caps:
            args += ["--trial-cap", cap]
        if force:
            args.append("--force")
        return args

    def init(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.ok(*self.init_args(*args, **kwargs))

    def reserve_args(
        self,
        task_id: str = "t",
        step: str = "s1",
        credits: int = 1,
        *,
        operation: str = "generate",
        trial: str | None = None,
        trial_count: int | None = None,
        downloads: int | None = None,
        metadata: dict[str, Any] | None = None,
        note: str | None = None,
    ) -> list[Any]:
        args: list[Any] = [
            "reserve",
            "--task-id",
            task_id,
            "--step-key",
            step,
            "--operation",
            operation,
            "--credits",
            credits,
        ]
        if trial is not None:
            args += ["--trial", trial]
        if trial_count is not None:
            args += ["--trial-count", trial_count]
        if downloads is not None:
            args += ["--downloads", downloads]
        if metadata is not None:
            args += ["--metadata-json", json.dumps(metadata, separators=(",", ":"))]
        if note is not None:
            args += ["--note", note]
        return args

    def reserve(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.ok(*self.reserve_args(*args, **kwargs))

    def commit_args(
        self,
        task_id: str = "t",
        step: str = "s1",
        status: str = "submitted",
        *,
        actual: int | None = None,
        evidence: dict[str, Any] | None = None,
        operator_id: str | None = None,
        project_id: str | None = None,
        error: str | None = None,
        reconcile: bool = False,
    ) -> list[Any]:
        args: list[Any] = [
            "commit",
            "--task-id",
            task_id,
            "--step-key",
            step,
            "--status",
            status,
        ]
        if actual is not None:
            args += ["--actual-credits", actual]
        if evidence is not None:
            args += ["--evidence-json", json.dumps(evidence, separators=(",", ":"))]
        if operator_id:
            args += ["--operator-id", operator_id]
        if project_id:
            args += ["--project-id", project_id]
        if error:
            args += ["--error", error]
        if reconcile:
            args.append("--reconcile-unknown")
        return args

    def commit(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.ok(*self.commit_args(*args, **kwargs))

    def status(self, task_id: str = "t") -> dict[str, Any]:
        return self.ok("status", "--task-id", task_id)

    def raw_ledger(self) -> dict[str, Any]:
        return json.loads(self.ledger.read_text(encoding="utf-8"))

    # ---------- required runtime tests ----------
    def test_two_concurrent_cli_reserves_cap10_each6_only_one_succeeds(self) -> None:
        self.init(budget=10, max_submissions=2)
        base = [
            PYTHON,
            "-B",
            str(LEDGER_SCRIPT),
            "--ledger",
            str(self.ledger),
            "reserve",
            "--task-id",
            "t",
            "--operation",
            "generate",
            "--credits",
            "6",
        ]
        procs = [
            subprocess.Popen(
                [*base, "--step-key", "s1"],
                cwd=str(WORKDIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                env=_child_env(),
            ),
            subprocess.Popen(
                [*base, "--step-key", "s2"],
                cwd=str(WORKDIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                env=_child_env(),
            ),
        ]
        results: list[tuple[int, str, str]] = []
        for proc in procs:
            stdout, stderr = proc.communicate(timeout=30)
            results.append((proc.returncode, stdout.strip(), stderr.strip()))

        returncodes = sorted(rc for rc, _, _ in results)
        self.assertEqual(returncodes, [0, 2], results)
        successes = [item for item in results if item[0] == 0]
        failures = [item for item in results if item[0] == 2]
        self.assertEqual(len(successes), 1, results)
        self.assertEqual(len(failures), 1, results)
        self.assertIn("BUDGET CAP EXCEEDED", failures[0][2], failures[0])

        status = self.status()
        self.assertEqual(status["spent_credits"] + status["reserved_credits"], 6)
        self.assertEqual(status["remaining_budget"], 4)
        self.assertEqual(len(status["steps"]), 1)
        self.assertTrue((self.ledger.parent / (self.ledger.name + ".lock")).exists())

    def test_submitted_abort_rejected_and_spent_not_decreased(self) -> None:
        self.init(budget=20, max_submissions=2)
        self.reserve(credits=5)
        self.commit(status="submitted")
        before = self.status()
        self.assertEqual(before["spent_credits"], 5)
        self.assertEqual(before["reserved_credits"], 0)
        self.assertEqual(before["steps"]["s1"]["status"], "submitted")
        self.assertTrue(before["steps"]["s1"]["was_submitted"])

        self.fail(
            *self.commit_args(status="aborted_before_submit"),
            expect="INVALID STATE TRANSITION",
        )
        after_abort = self.status()
        self.assertEqual(after_abort["spent_credits"], 5)
        self.assertEqual(after_abort["steps"]["s1"]["status"], "submitted")

        # A post-submit failure keeps the potential spend.
        self.commit(status="failed", error="phone result failed after submission")
        after_failure = self.status()
        self.assertEqual(after_failure["spent_credits"], 5)
        self.assertEqual(after_failure["steps"]["s1"]["status"], "failed")
        self.assertTrue(after_failure["halted"])
        self.fail(*self.commit_args(status="success"), expect="INVALID STATE TRANSITION")
        self.assertEqual(self.status()["spent_credits"], 5)

    def test_zero_credit_success_same_key_and_init_force_blocked(self) -> None:
        self.init(budget=0, max_submissions=2)
        self.reserve(credits=0)
        self.commit(status="success")
        status = self.status()
        self.assertEqual(status["spent_credits"], 0)
        self.assertEqual(status["steps"]["s1"]["status"], "success")
        self.assertEqual(status["resource_usage"]["submissions_used"], 1)

        self.fail(
            *self.reserve_args(step="s1", credits=0),
            expect="IDEMPOTENCY BLOCK",
        )
        self.reserve(step="s2", credits=0)
        self.commit(step="s2", status="success")
        self.fail(
            *self.reserve_args(step="s3", credits=0),
            expect="MAX SUBMISSIONS EXCEEDED",
        )
        self.fail(
            *self.init_args(budget=99, force=True),
            expect="Refusing to reset",
        )
        status = self.status()
        self.assertEqual(status["spent_credits"], 0)
        self.assertEqual(set(status["steps"]), {"s1", "s2"})
        self.assertGreaterEqual(len(self.raw_ledger()["tasks"]["t"]["steps"]["s1"]["history"]), 2)

        # --force is also rejected when only an aborted attempt exists.
        self.init(task_id="t2", budget=10)
        self.reserve(task_id="t2", step="a1", credits=1)
        self.commit(task_id="t2", step="a1", status="aborted_before_submit")
        self.fail(
            *self.reserve_args(task_id="t2", step="a1", credits=1),
            expect="IDEMPOTENCY BLOCK",
        )
        self.fail(
            *self.init_args(task_id="t2", budget=10, force=True),
            expect="Refusing to reset",
        )

    def test_unknown_and_submitting_block_new_reserve(self) -> None:
        self.init(task_id="u", budget=100, max_submissions=3)
        self.reserve(task_id="u", credits=0)
        self.commit(task_id="u", status="unknown", error="ambiguous click result")
        unknown_status = self.status("u")
        self.assertTrue(unknown_status["halted"])
        self.assertEqual(unknown_status["unresolved_unknown"], ["s1"])
        self.fail(
            *self.reserve_args(task_id="u", step="s2", credits=0),
            expect="TASK HALTED",
        )
        self.fail(
            *self.commit_args(task_id="u", status="success"),
            expect="UNKNOWN RECONCILE REQUIRED",
        )
        self.fail(
            *self.commit_args(task_id="u", status="success", reconcile=True),
            expect="REQUIRES --evidence-json",
        )
        self.fail(
            "clear-halt",
            "--task-id",
            "u",
            "--reason",
            "trying before reconcile",
            "--human-review",
            expect="UNKNOWN STATE UNRESOLVED",
        )

        reconciled = self.commit(
            task_id="u",
            status="success",
            reconcile=True,
            evidence={"ui": "human confirmed success"},
            operator_id="op1",
        )
        self.assertTrue(reconciled["halted"])
        self.assertEqual(reconciled["spent_credits"], 0)
        after_reconcile = self.status("u")
        self.assertEqual(after_reconcile["steps"]["s1"]["status"], "success")
        self.assertEqual(after_reconcile["unresolved_unknown"], [])
        self.fail(
            *self.reserve_args(task_id="u", step="s2", credits=0),
            expect="TASK HALTED",
        )
        self.fail(
            "clear-halt",
            "--task-id",
            "u",
            "--reason",
            "no flag",
            expect="HUMAN REVIEW REQUIRED",
        )
        self.ok(
            "clear-halt",
            "--task-id",
            "u",
            "--reason",
            "reviewed UI receipt",
            "--human-review",
        )
        self.assertFalse(self.status("u")["halted"])
        self.fail(
            *self.reserve_args(task_id="u", step="s1", credits=0),
            expect="IDEMPOTENCY BLOCK",
        )
        self.reserve(task_id="u", step="s2", credits=0)

        raw = self.raw_ledger()["tasks"]["u"]
        self.assertGreaterEqual(len(raw["halt_history"]), 1)
        events = [entry["event"] for entry in raw["steps"]["s1"]["history"]]
        self.assertIn("commit", events)
        self.assertIn("reconcile", events)

        self.init(task_id="p", budget=100, max_submissions=3)
        self.reserve(task_id="p", credits=5)
        self.commit(task_id="p", status="submitting")
        submitting_status = self.status("p")
        self.assertEqual(submitting_status["spent_credits"], 0)
        self.assertEqual(submitting_status["reserved_credits"], 5)
        self.assertEqual(submitting_status["submitting"], ["s1"])
        self.fail(
            *self.reserve_args(task_id="p", step="s2", credits=5),
            expect="SUBMISSION IN PROGRESS",
        )
        self.fail(
            *self.commit_args(task_id="p", status="aborted_before_submit"),
            expect="INVALID STATE TRANSITION",
        )
        self.fail(
            *self.commit_args(task_id="p", status="failed"),
            expect="INVALID STATE TRANSITION",
        )
        self.commit(task_id="p", status="submitted")
        submitted_status = self.status("p")
        self.assertEqual(submitted_status["spent_credits"], 5)
        self.assertEqual(submitted_status["reserved_credits"], 0)
        self.reserve(task_id="p", step="s2", credits=5)

    def test_trial_download_and_max_submission_caps(self) -> None:
        self.init(
            task_id="tr",
            budget=0,
            max_submissions=5,
            trial_caps=("trial_a=1",),
            download_cap=0,
        )
        reserved = self.reserve(
            task_id="tr",
            credits=0,
            trial="trial_a",
            trial_count=1,
            metadata={"raw": 30, "trial": "trial_a", "ui": 0},
        )
        self.assertEqual(reserved["resource_usage"]["trial_usage"], {"trial_a": 1})
        self.fail(
            *self.reserve_args(
                task_id="tr", step="s2", credits=0, trial="trial_a", trial_count=1
            ),
            expect="TRIAL CAP EXCEEDED",
        )
        self.fail(
            *self.reserve_args(task_id="tr", step="s2", credits=0, trial="trial_b"),
            expect="TRIAL NOT ALLOWED",
        )
        self.fail(
            *self.reserve_args(task_id="tr", step="s2", credits=0, downloads=1),
            expect="DOWNLOAD CAP EXCEEDED",
        )

        self.init(
            task_id="tr2",
            budget=0,
            max_submissions=5,
            trial_caps=("trial_a=2", "trial_b=0"),
        )
        self.reserve(task_id="tr2", credits=0, trial="trial_a", trial_count=2)
        self.fail(
            *self.reserve_args(task_id="tr2", step="s2", credits=0, trial="trial_a"),
            expect="TRIAL CAP EXCEEDED",
        )
        self.fail(
            *self.reserve_args(task_id="tr2", step="s2", credits=0, trial="trial_b"),
            expect="TRIAL CAP EXCEEDED",
        )

        self.init(task_id="dl", budget=10, max_submissions=5, download_cap=1)
        self.reserve(task_id="dl", credits=1, downloads=1)
        self.fail(
            *self.reserve_args(task_id="dl", step="s2", credits=1, downloads=1),
            expect="DOWNLOAD CAP EXCEEDED",
        )
        self.reserve(task_id="dl", step="s2", credits=1, downloads=0)

        self.init(task_id="ms", budget=0, max_submissions=1)
        self.reserve(task_id="ms", credits=0)
        self.fail(
            *self.reserve_args(task_id="ms", step="s2", credits=0),
            expect="MAX SUBMISSIONS EXCEEDED",
        )
        self.commit(task_id="ms", status="aborted_before_submit")
        self.reserve(task_id="ms", step="s2", credits=0)
        self.commit(task_id="ms", step="s2", status="submitting")
        self.assertEqual(
            self.status("ms")["resource_usage"]["submissions_used"],
            1,
        )
        self.commit(task_id="ms", step="s2", status="submitted")
        self.fail(
            *self.reserve_args(task_id="ms", step="s3", credits=0),
            expect="MAX SUBMISSIONS EXCEEDED",
        )
        self.assertEqual(self.status("ms")["spent_credits"], 0)

        # CLI defaults: max-submissions=1, trial caps empty, download cap 0.
        self.ok("init-task", "--task-id", "defaults", "--budget-cap", "0")
        self.fail(
            *self.reserve_args(task_id="defaults", step="s1", credits=0, trial="a"),
            expect="TRIAL NOT ALLOWED",
        )
        self.fail(
            *self.reserve_args(task_id="defaults", step="s1", credits=0, downloads=1),
            expect="DOWNLOAD CAP EXCEEDED",
        )
        self.reserve(task_id="defaults", step="s1", credits=0)
        self.fail(
            *self.reserve_args(task_id="defaults", step="s2", credits=0),
            expect="MAX SUBMISSIONS EXCEEDED",
        )

    def test_normal_reserve_submitting_submitted_success(self) -> None:
        self.init(budget=50, max_submissions=2)
        reserved = self.reserve(credits=10, metadata={"raw": 10, "ui": 10})
        for key in ("ok", "action", "step_key", "credits_reserved", "remaining_budget"):
            self.assertIn(key, reserved)
        self.assertEqual(reserved["credits_reserved"], 10)
        self.assertEqual(reserved["remaining_budget"], 40)

        submitting = self.commit(status="submitting")
        for key in ("ok", "action", "step_key", "status", "halted", "spent_credits", "remaining_budget"):
            self.assertIn(key, submitting)
        status = self.status()
        self.assertEqual(status["spent_credits"], 0)
        self.assertEqual(status["reserved_credits"], 10)

        self.commit(status="submitted")
        status = self.status()
        self.assertEqual(status["spent_credits"], 10)
        self.assertEqual(status["reserved_credits"], 0)
        self.assertEqual(status["steps"]["s1"]["status"], "submitted")
        self.assertTrue(status["steps"]["s1"]["was_submitted"])

        self.commit(status="success", actual=10, evidence={"receipt": "10", "readback": 10})
        status = self.status()
        self.assertEqual(status["spent_credits"], 10)
        self.assertEqual(status["steps"]["s1"]["status"], "success")
        self.assertEqual(status["steps"]["s1"]["actual_credits"], 10)
        self.assertEqual(status["steps"]["s1"]["evidence"], {"receipt": "10", "readback": 10})

        self.fail(*self.commit_args(status="success"), expect="STATE OVERWRITE BLOCKED")
        self.fail(
            *self.commit_args(status="aborted_before_submit"),
            expect="INVALID STATE TRANSITION",
        )
        self.fail(*self.reserve_args(step="s1", credits=1), expect="IDEMPOTENCY BLOCK")
        self.reserve(step="s2", credits=10)
        self.commit(step="s2", status="success")
        final = self.status()
        self.assertEqual(final["spent_credits"], 20)
        self.assertEqual(final["resource_usage"]["submissions_used"], 2)

    def test_illegal_negative_values(self) -> None:
        self.fail(
            "init-task",
            "--task-id",
            "neg",
            "--budget-cap",
            "-1",
            expect="non-negative",
        )
        self.fail(
            *self.init_args(task_id="neg", budget=10, max_submissions=-1),
            expect="non-negative",
        )
        self.fail(
            *self.init_args(task_id="neg", budget=10, download_cap=-1),
            expect="non-negative",
        )
        self.fail(
            "init-task",
            "--task-id",
            "neg",
            "--budget-cap",
            "10",
            "--trial-cap",
            "bad=-1",
            expect="non-negative",
        )
        self.fail(
            "init-task",
            "--task-id",
            "neg",
            "--budget-cap",
            "10",
            "--trial-cap",
            "novalue",
            expect="NAME=COUNT",
        )
        self.fail(
            "init-task",
            "--task-id",
            "neg",
            "--budget-cap",
            "10",
            "--trial-cap",
            "a=1",
            "--trial-cap",
            "a=2",
            expect="Duplicate",
        )

        self.init(task_id="neg", budget=10, max_submissions=2, trial_caps=("a=1",), download_cap=1)
        self.fail(
            *self.reserve_args(task_id="neg", step="x", credits=-1),
            expect="non-negative",
        )
        self.fail(
            *self.reserve_args(task_id="neg", step="x", credits=1, downloads=-1),
            expect="non-negative",
        )
        self.fail(
            *self.reserve_args(
                task_id="neg", step="x", credits=1, trial="a", trial_count=0
            ),
            expect=">= 1",
        )
        self.fail(
            *self.reserve_args(
                task_id="neg", step="x", credits=1, trial="a", trial_count=-1
            ),
            expect="non-negative",
        )
        self.fail(
            *self.reserve_args(task_id="neg", step="x", credits=1, trial_count=1),
            expect="requires --trial",
        )
        self.reserve(task_id="neg", step="ok", credits=1)
        self.fail(
            *self.commit_args(task_id="neg", step="ok", status="success", actual=-1),
            expect="non-negative",
        )
        status = self.status("neg")
        self.assertEqual(status["steps"]["ok"]["status"], "reserved")
        self.assertEqual(status["spent_credits"], 0)

    def test_legacy_schema_readable_and_protected(self) -> None:
        now = "2026-01-01T00:00:00+00:00"

        def step(key: str, credits: int, status: str, was_submitted: bool | None) -> dict[str, Any]:
            item: dict[str, Any] = {
                "step_key": key,
                "operation": "generate",
                "credits": credits,
                "status": status,
                "operator_id": "",
                "project_id": "",
                "note": "",
                "reserved_at": now,
                "updated_at": now,
            }
            if was_submitted is not None:
                item["was_submitted"] = was_submitted
            return item

        legacy = {
            "schema_version": 1,
            "tasks": {
                "legacy-unknown": {
                    "task_id": "legacy-unknown",
                    "region": "cn",
                    "budget_cap": 30,
                    "spent_credits": 9,
                    "reserved_credits": 3,
                    "halted": False,
                    "halt_reason": "",
                    "note": "old",
                    "created_at": now,
                    "updated_at": now,
                    "steps": {
                        "old-success": step("old-success", 5, "success", True),
                        "old-reserved": step("old-reserved", 3, "reserved", False),
                        # Missing was_submitted on purpose: unknown still counts.
                        "old-unknown": step("old-unknown", 4, "unknown", None),
                    },
                },
                "legacy-normal": {
                    "task_id": "legacy-normal",
                    "region": "cn",
                    "budget_cap": 20,
                    "spent_credits": 5,
                    "reserved_credits": 3,
                    "halted": False,
                    "halt_reason": "",
                    "note": "old",
                    "created_at": now,
                    "updated_at": now,
                    "steps": {
                        "old-success": step("old-success", 5, "success", True),
                        "old-reserved": step("old-reserved", 3, "reserved", False),
                    },
                },
                "legacy-totals": {
                    "task_id": "legacy-totals",
                    "region": "cn",
                    "budget_cap": 10,
                    "spent_credits": 2,
                    "reserved_credits": 1,
                    "halted": False,
                    "halt_reason": "",
                    "note": "old totals without steps",
                    "created_at": now,
                    "updated_at": now,
                    "steps": {},
                },
            },
        }
        self.ledger.write_text(json.dumps(legacy, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        unknown_status = self.status("legacy-unknown")
        self.assertEqual(unknown_status["schema_version"], 1)
        self.assertEqual(unknown_status["spent_credits"], 9)
        self.assertEqual(unknown_status["reserved_credits"], 3)
        self.assertEqual(unknown_status["remaining_budget"], 18)
        self.assertEqual(unknown_status["unresolved_unknown"], ["old-unknown"])
        self.assertFalse(unknown_status["resource_usage"]["enforced"])
        self.assertEqual(set(unknown_status["steps"]), {"old-success", "old-reserved", "old-unknown"})
        self.fail(
            *self.reserve_args(task_id="legacy-unknown", step="new", credits=10),
            expect="UNKNOWN STATE UNRESOLVED",
        )
        self.fail(
            *self.commit_args(task_id="legacy-unknown", step="old-unknown", status="success"),
            expect="UNKNOWN RECONCILE REQUIRED",
        )
        self.commit(
            task_id="legacy-unknown",
            step="old-unknown",
            status="success",
            reconcile=True,
            evidence={"ui": "legacy human confirmation"},
        )
        self.assertEqual(self.status("legacy-unknown")["spent_credits"], 9)
        self.reserve(task_id="legacy-unknown", step="new", credits=10)

        normal_status = self.status("legacy-normal")
        self.assertEqual(normal_status["spent_credits"], 5)
        self.assertEqual(normal_status["reserved_credits"], 3)
        self.assertEqual(normal_status["remaining_budget"], 12)
        self.fail(
            *self.reserve_args(task_id="legacy-normal", step="old-success", credits=0),
            expect="IDEMPOTENCY BLOCK",
        )
        self.fail(
            *self.reserve_args(task_id="legacy-normal", step="new", credits=13),
            expect="BUDGET CAP EXCEEDED",
        )
        self.reserve(task_id="legacy-normal", step="new", credits=10)
        after_new = self.status("legacy-normal")
        self.assertEqual(after_new["spent_credits"], 5)
        self.assertEqual(after_new["reserved_credits"], 13)
        self.assertEqual(after_new["remaining_budget"], 2)
        self.fail(
            *self.reserve_args(task_id="legacy-normal", step="new2", credits=1, trial="a"),
            expect="Resource budgets are only enforced",
        )
        self.fail(
            *self.commit_args(task_id="legacy-normal", step="old-success", status="success"),
            expect="STATE OVERWRITE BLOCKED",
        )
        self.fail(
            *self.commit_args(task_id="legacy-normal", step="old-success", status="failed"),
            expect="INVALID STATE TRANSITION",
        )

        totals_status = self.status("legacy-totals")
        self.assertEqual(totals_status["spent_credits"], 2)
        self.assertEqual(totals_status["reserved_credits"], 1)
        self.fail(
            *self.reserve_args(task_id="legacy-totals", step="x", credits=8),
            expect="BUDGET CAP EXCEEDED",
        )
        self.reserve(task_id="legacy-totals", step="x", credits=7)

        self.init(task_id="new-schema", budget=5, max_submissions=1)
        root = self.raw_ledger()
        self.assertEqual(root["schema_version"], 2)
        self.assertEqual(root["tasks"]["new-schema"]["schema_version"], 2)
        self.assertNotIn("schema_version", root["tasks"]["legacy-unknown"])
        self.assertEqual(self.status("legacy-unknown")["schema_version"], 1)
        self.assertEqual(self.status("new-schema")["schema_version"], 2)

    def test_actual_credits_and_evidence_receipts(self) -> None:
        self.init(budget=20, max_submissions=2)
        metadata = {"raw": 25, "trial": "trial_a", "ui": 5, "preflight": {"ok": True}}
        self.reserve(credits=5, metadata=metadata)
        self.assertEqual(self.status()["steps"]["s1"]["metadata"], metadata)
        self.fail(
            "reserve",
            "--task-id",
            "t",
            "--step-key",
            "bad",
            "--operation",
            "generate",
            "--credits",
            "0",
            "--metadata-json",
            "{bad",
            expect="valid JSON",
        )
        self.fail(
            "reserve",
            "--task-id",
            "t",
            "--step-key",
            "bad",
            "--operation",
            "generate",
            "--credits",
            "0",
            "--metadata-json",
            "[1,2]",
            expect="JSON object",
        )

        self.commit(status="submitting")
        self.commit(status="submitted")
        self.fail(
            "commit",
            "--task-id",
            "t",
            "--step-key",
            "s1",
            "--status",
            "success",
            "--evidence-json",
            "{bad",
            expect="valid JSON",
        )
        self.fail(
            *self.commit_args(status="success", reconcile=True, evidence={"x": 1}),
            expect="only valid when the current status is unknown",
        )
        self.assertEqual(self.status()["steps"]["s1"]["status"], "submitted")
        self.commit(
            status="success",
            actual=5,
            evidence={"receipt": "abc", "readback": 5},
        )
        final = self.status()["steps"]["s1"]
        self.assertEqual(final["actual_credits"], 5)
        self.assertEqual(final["evidence"], {"receipt": "abc", "readback": 5})

        self.init(task_id="over", budget=100, max_submissions=2)
        self.reserve(task_id="over", credits=5)
        self.commit(task_id="over", status="submitted")
        over = self.commit(
            task_id="over",
            status="success",
            actual=8,
            evidence={"receipt": "8"},
        )
        self.assertTrue(over["halted"])
        self.assertEqual(over["spent_credits"], 8)
        over_status = self.status("over")
        self.assertEqual(over_status["spent_credits"], 8)
        self.assertEqual(over_status["remaining_budget"], 92)
        self.assertTrue(over_status["steps"]["s1"]["actual_exceeds_reserved"])
        self.fail(
            *self.reserve_args(task_id="over", step="s2", credits=1),
            expect="TASK HALTED",
        )

        self.init(task_id="zero", budget=100, max_submissions=2)
        self.reserve(task_id="zero", credits=5)
        zero = self.commit(
            task_id="zero",
            status="success",
            actual=0,
            evidence={"receipt": "0"},
        )
        self.assertFalse(zero["halted"])
        self.assertEqual(zero["spent_credits"], 5)
        self.assertEqual(zero["reserved_credits"], 0)
        zero_status = self.status("zero")
        self.assertEqual(zero_status["spent_credits"], 5)
        self.assertTrue(zero_status["steps"]["s1"]["actual_below_reserved"])

    def test_clear_halt_requires_human_review_and_preserves_old_steps(self) -> None:
        self.init(budget=10, max_submissions=3)
        self.reserve(credits=1)
        self.commit(status="unknown", error="ambiguous click", operator_id="op1")
        self.fail(
            "clear-halt",
            "--task-id",
            "t",
            "--reason",
            "reviewed",
            expect="HUMAN REVIEW REQUIRED",
        )
        self.fail(
            "clear-halt",
            "--task-id",
            "t",
            "--reason",
            "reviewed",
            "--human-review",
            expect="UNKNOWN STATE UNRESOLVED",
        )
        self.commit(
            status="success",
            reconcile=True,
            evidence={"ui": "success confirmed"},
            operator_id="op1",
        )
        self.ok(
            "clear-halt",
            "--task-id",
            "t",
            "--reason",
            "human verified UI receipt",
            "--human-review",
        )
        status = self.status()
        self.assertFalse(status["halted"])
        self.fail(*self.reserve_args(step="s1", credits=0), expect="IDEMPOTENCY BLOCK")
        self.reserve(step="s2", credits=1)
        raw = self.raw_ledger()["tasks"]["t"]
        self.assertGreaterEqual(len(raw["halt_history"]), 2)
        self.assertEqual(raw["steps"]["s1"]["status"], "success")
        events = [entry["event"] for entry in raw["steps"]["s1"]["history"]]
        self.assertIn("commit", events)
        self.assertIn("reconcile", events)

    def test_lock_timeout_blocks_another_process(self) -> None:
        self.assertLessEqual(tripo_ledger.LOCK_TIMEOUT_SECONDS, 10.0)
        started = time.monotonic()
        with tripo_ledger._ledger_lock(self.ledger):
            rc, body, stdout, stderr = self.cli(
                "reserve",
                "--task-id",
                "t",
                "--step-key",
                "s1",
                "--operation",
                "generate",
                "--credits",
                "0",
                timeout=15,
            )
        elapsed = time.monotonic() - started
        self.assertEqual(rc, 2, (body, stdout, stderr))
        self.assertIn("LEDGER LOCK TIMEOUT", stderr)
        self.assertLess(elapsed, 12.0)

    def test_estimate_still_works(self) -> None:
        result = self.ok("estimate", "--kind", "generate")
        self.assertIn("estimate", result)
        self.assertIn("net_credits", result["estimate"])
        self.assertIsInstance(result["estimate"]["net_credits"], int)


if __name__ == "__main__":
    unittest.main(verbosity=2)
