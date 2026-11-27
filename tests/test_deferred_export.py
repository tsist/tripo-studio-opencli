"""Offline Studio export regressions using a temporary real ledger."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/tripo-studio-opencli/scripts"
sys.path.insert(0, str(SCRIPTS))
import tripo_export


class FakeStudio:
    def __init__(self):
        self.events = []
        self.projects = {}
        self.progress = {}

    @staticmethod
    def _ledger(path, *args):
        result = subprocess.run(
            [sys.executable, "-B", str(SCRIPTS / "tripo_ledger.py"), "--ledger", str(path), *args],
            capture_output=True, text=True, encoding="utf-8", timeout=10,
        )
        if result.returncode:
            raise RuntimeError(result.stderr)
        return json.loads(result.stdout)

    _extract_json = staticmethod(json.loads)

    @staticmethod
    def _ensure_daemon(profile):
        assert profile == "approved-profile"

    @staticmethod
    def _fetch_preflight(profile, region):
        return {"quota": {"download_quota_remaining": 5}}

    def _opencli(self, profile, site, action, *args, **kwargs):
        self.events.append((action, args))
        assert site == "tripo-studio"
        if action == "project":
            assert "--operator-id" not in args
            return json.dumps([self.projects[args[0]]])
        if action == "progress":
            operator = args[args.index("--ids") + 1]
            return json.dumps([{"operator_id": operator, "status": self.progress[operator]}])
        assert action == "export-url"
        operator = "export-" + args[0]
        if "--submit-only" in args:
            assert "--confirm-export" in args
            self.progress[operator] = "running"
            return json.dumps([{"operator_id": operator, "project_id": args[0], "file_name": "same-name.obj",
                                "state": "submitted", "is_owner": True, "estimated_credits": 0, "format": "obj"}])
        assert "--confirm-export" not in args and args[args.index("--resume-operator-id") + 1] == operator
        return json.dumps([{"operator_id": operator, "project_id": args[0],
                            "file_name": "same-name.obj", "model_url": "https://example.invalid/unused"}])


class FakeDownload:
    def __init__(self):
        self.jobs = {}

    def doctor(self):
        pass

    def start(self, unused_url, destination, **options):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("v 0 0 0\nv 0 1 0\nv 1 0 0\nf 1 2 3\n", encoding="utf-8")
        job_id = f"job-{len(self.jobs) + 1}"
        self.jobs[job_id] = {"id": job_id, "status": "complete", "destination": str(destination)}
        return self.jobs[job_id]

    def status(self, job_id):
        return self.jobs[job_id]


class DeferredExportTests(unittest.TestCase):
    def setUp(self):
        temp = self.enterContext(tempfile.TemporaryDirectory(prefix="tripo-export-test-"))
        self.runtime = Path(temp)
        self.studio = FakeStudio()
        self.download = FakeDownload()
        self.enterContext(patch.object(tripo_export, "SmartDownloadBridge", return_value=self.download))
        self.ledger = self.runtime / "ledger.json"
        self.studio._ledger(self.ledger, "init-task", "--task-id", "batch", "--budget-cap", "0",
                            "--max-submissions", "5", "--download-cap", "5", "--region", "cn",
                            "--profile", "approved-profile")
        for project in ("project-1", "project-2"):
            self.studio.projects[project] = {
                "project_id": project, "operator_id": f"source-{project}", "is_owner": True, "status": "success",
            }

    def arguments(self, step_key="export-1", project="project-1", **changes):
        return SimpleNamespace(**{
            "task_id": "batch", "step_key": step_key, "project_id": project, "region": "cn",
            "profile": "approved-profile", "ledger": str(self.ledger), "runtime_dir": str(self.runtime),
            "model_format": "obj", "fbx_preset": "blender", "texture_packaging": "embedded", "texture_size": 2048,
            "name": "", "with_animation": False, "enable_bake_animation": False, "animation_names": [],
            "pivot_to_center_bottom": False, "pack_uv": False, "allow_nonowner_spend": False, "execute_spend": True,
            "submit_only": True, "source_operator_id": f"source-{project}", "resume_export": False, "download_job": "",
            **changes,
        })

    def ledger_status(self):
        return self.studio._ledger(self.ledger, "status", "--task-id", "batch")

    def test_short_submission_then_pending_snapshot_and_download_by_original_operator(self):
        first = tripo_export.export(self.studio, self.arguments())
        second = tripo_export.export(self.studio, self.arguments("export-2", "project-2"))
        self.assertEqual(len(self.download.jobs), 0)
        self.assertNotEqual(first["destination"], second["destination"])
        self.assertEqual(Path(first["destination"]).name, Path(second["destination"]).name)
        self.assertEqual(self.ledger_status()["steps"]["export-1"]["status"], "submitted")

        resume = self.arguments(submit_only=False, execute_spend=False, resume_export=True)
        self.studio.projects["project-1"]["operator_id"] = "different-current-source"
        self.assertTrue(tripo_export.export(self.studio, resume)["export_pending"])
        self.assertEqual(self.studio.events[-1][0], "progress")
        self.studio.progress[first["operator_id"]] = "success"
        downloaded = tripo_export.export(self.studio, resume)
        completed = tripo_export.export(self.studio, self.arguments(
            submit_only=False, execute_spend=False, download_job=downloaded["job"]["id"],
        ))
        self.assertEqual(completed["manifest"]["source_operator_id"], first["source_operator_id"])
        self.assertEqual(completed["manifest"]["source_check"], "studio_current_before_submit")
        self.assertEqual(len(completed["manifest"]["artifact"]["sha256"]), 64)
        self.assertEqual(self.ledger_status()["steps"]["export-1"]["status"], "success")
        self.assertEqual(self.ledger_status()["steps"]["export-2"]["status"], "submitted")
        self.assertEqual(sum("--confirm-export" in args for _, args in self.studio.events), 2)

    def test_stale_or_foreign_current_source_never_submits(self):
        for data in ({"operator_id": "stale-operator"}, {"project_id": "foreign-project"}, {"is_owner": False}):
            original = self.studio.projects["project-1"]
            self.studio.projects["project-1"] = {**original, **data}
            with self.assertRaisesRegex(RuntimeError, "current Studio operator|Only owner"):
                tripo_export.export(self.studio, self.arguments())
            self.studio.projects["project-1"] = original
        self.assertEqual({}, self.ledger_status()["steps"])
        self.assertTrue(all(action == "project" for action, _ in self.studio.events))

    def test_in_flight_same_project_blocks_only_its_export(self):
        self.studio._ledger(
            self.ledger, "reserve", "--task-id", "batch", "--step-key", "retopo", "--operation", "postprocess_retopology",
            "--credits", "0", "--metadata-json", json.dumps({"project_id": "project-1", "studio_profile": "approved-profile"}),
        )
        with self.assertRaisesRegex(RuntimeError, "active step"):
            tripo_export.export(self.studio, self.arguments("other-export"))
        first = tripo_export.export(self.studio, self.arguments("export-2", "project-2"))
        with self.assertRaisesRegex(RuntimeError, "another Studio profile"):
            tripo_export.export(self.studio, self.arguments(
                "export-2", "project-2", submit_only=False, execute_spend=False, resume_export=True, profile="other-profile",
            ))
        self.assertEqual(self.ledger_status()["steps"]["export-2"]["operator_id"], first["operator_id"])
        self.assertEqual(sum("--confirm-export" in args for _, args in self.studio.events), 1)

    def test_source_change_after_submission_keeps_receipt_and_blocks_download(self):
        original = self.studio._opencli

        def switch_source_after_submit(*args, **kwargs):
            reply = original(*args, **kwargs)
            if '--confirm-export' in args:
                self.studio.projects['project-1']['operator_id'] = 'different-current-source'
            return reply

        with patch.object(self.studio, '_opencli', side_effect=switch_source_after_submit):
            with self.assertRaisesRegex(RuntimeError, 'could not be confirmed again'):
                tripo_export.export(self.studio, self.arguments())
        step = self.ledger_status()['steps']['export-1']
        self.assertEqual(step['status'], 'unknown')
        self.assertEqual(step['operator_id'], 'export-project-1')
        with self.assertRaisesRegex(RuntimeError, 'source is unresolved'):
            tripo_export.export(self.studio, self.arguments(submit_only=False, execute_spend=False, resume_export=True))
        self.assertEqual(self.download.jobs, {})

    def test_recover_lost_local_record_once_from_durable_submit_evidence(self):
        with patch.object(tripo_export, "_new_json", side_effect=OSError("simulated record write interruption")):
            with self.assertRaisesRegex(OSError, "simulated record"):
                tripo_export.export(self.studio, self.arguments())
        step = self.ledger_status()["steps"]["export-1"]
        self.assertEqual(step["status"], "submitted")
        self.assertEqual(step["evidence"]["file_name"], "same-name.obj")
        self.studio.progress[step["operator_id"]] = "success"
        result = tripo_export.export(self.studio, self.arguments(
            submit_only=False, execute_spend=False, resume_export=True,
        ))
        self.assertTrue(result["download_running"])
        self.assertEqual(sum("--confirm-export" in args for _, args in self.studio.events), 1)

    def test_matching_terminal_cloud_failure_halts_the_task(self):
        first = tripo_export.export(self.studio, self.arguments())
        self.studio.progress[first["operator_id"]] = "failed"
        with self.assertRaisesRegex(RuntimeError, "ended with status 'failed'"):
            tripo_export.export(self.studio, self.arguments(
                submit_only=False, execute_spend=False, resume_export=True,
            ))
        status = self.ledger_status()
        self.assertTrue(status["halted"])
        self.assertEqual(status["steps"]["export-1"]["status"], "failed")
        self.assertEqual(len(self.download.jobs), 0)

    def test_legacy_submitted_export_resumes_without_source_field(self):
        args = self.arguments(submit_only=False, execute_spend=False, source_operator_id="", resume_export=True)
        params = tripo_export._params(args)
        for key in ("source_operator_id", "animation_names", "pivot_to_center_bottom", "pack_uv"):
            del params[key]
        self.studio._ledger(
            self.ledger, "reserve", "--task-id", "batch", "--step-key", args.step_key,
            "--operation", "postprocess_export", "--credits", "0", "--downloads", "1",
            "--metadata-json", json.dumps({"export_params": params, "studio_profile": "approved-profile"}),
        )
        self.studio._ledger(
            self.ledger, "commit", "--task-id", "batch", "--step-key", args.step_key, "--status", "submitted",
            "--operator-id", "export-project-1", "--project-id", args.project_id,
        )
        downloaded = tripo_export.export(self.studio, args)
        self.assertEqual(Path(downloaded["job"]["destination"]).parent.resolve(), (self.runtime / "batch").resolve())
        self.assertEqual([action for action, _ in self.studio.events], ["export-url"])
        completed = tripo_export.export(self.studio, self.arguments(
            submit_only=False, execute_spend=False, source_operator_id="", download_job=downloaded["job"]["id"],
        ))
        self.assertNotIn("source_operator_id", completed["manifest"])
        self.assertEqual(self.ledger_status()["steps"][args.step_key]["status"], "success")


if __name__ == "__main__":
    unittest.main()
