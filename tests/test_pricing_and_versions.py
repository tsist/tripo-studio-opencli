"""Offline tests for latest Tripo model defaults (H3.5 Preview, P2.0, Rigging v3.0),
pricing math, strikethrough submit credit parsing, and StudioUI version selection.
"""
from __future__ import annotations

import json
import sys
import types
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/tripo-studio-opencli/scripts"
sys.path.insert(0, str(SCRIPTS))

import tripo_actions as actions
import tripo_pricing as pricing
import tripo_studio_driver as driver
from tripo_ui import StudioUI, _version_option_labels


class LatestModelPricingAndDefaultsTests(unittest.TestCase):
    def test_region_aware_hd_defaults_and_aliases(self) -> None:
        self.assertEqual(pricing.default_hd_version("global"), "v3.5-20261108")
        self.assertEqual(pricing.default_hd_version("cn"), "v3.1-20260211")
        self.assertEqual(pricing.resolve_hd_version(None, "global"), "v3.5-20261108")
        self.assertEqual(pricing.resolve_hd_version(None, "cn"), "v3.1-20260211")
        self.assertEqual(pricing.resolve_hd_version("latest", "cn"), "v3.5-20261108")
        self.assertEqual(pricing.resolve_hd_version("H3.5", "cn"), "v3.5-20261108")
        self.assertEqual(pricing.resolve_hd_version("h3.1", "global"), "v3.1-20260211")
        self.assertEqual(pricing.resolve_lp_version(None), "Nexus-v2.0-20260801")
        self.assertEqual(pricing.resolve_lp_version("p2"), "Nexus-v2.0-20260801")
        self.assertEqual(pricing.resolve_lp_version("p1"), "Nexus-v1.0-20260214")

    def test_region_aware_rig_defaults(self) -> None:
        self.assertEqual(pricing.resolve_rig_version(None, "biped", "global"), "v3.0-20260909")
        self.assertEqual(pricing.resolve_rig_version(None, "biped", "cn"), "v1.0-20240301")
        self.assertEqual(pricing.resolve_rig_version(None, "animal", "global"), "v2.5-20260210")
        self.assertEqual(pricing.resolve_rig_version(None, "animal", "cn"), "v2.5-20260210")
        self.assertEqual(pricing.resolve_rig_version("v3.0", "biped", "cn"), "v3.0-20260909")

    def test_h35_pricing_matches_live_studio_formula(self) -> None:
        # Global live H3.5 default: texture=True, 4K detailed (+10), PBR (+5), detailed geo (+15) -> 60 (orig 130)
        est_pbr = pricing.estimate_generate(
            tier="hd",
            hd_version="v3.5-20261108",
            texture=True,
            texture_quality="detailed",
            pbr=True,
            geometry_quality="detailed",
        )
        self.assertEqual(est_pbr.net_credits, 60)
        self.assertEqual(est_pbr.raw_credits, 60)

        # Without PBR: 30 + 10 + 15 = 55
        est_default = pricing.estimate_generate(tier="hd")
        self.assertEqual(est_default.net_credits, 55)

        # Without texture: base is still GenerateBaseH35 (30) + GeoDetailed (15) = 45
        est_notex = pricing.estimate_generate(
            tier="hd",
            hd_version="v3.5-20261108",
            texture=False,
            geometry_quality="detailed",
        )
        self.assertEqual(est_notex.net_credits, 45)

        # Extreme 8K + PBR + detailed geo: 30 + 20 + 5 + 15 = 70
        est_8k = pricing.estimate_generate(
            tier="hd",
            hd_version="h3.5",
            texture=True,
            texture_quality="extreme",
            pbr=True,
            geometry_quality="detailed",
        )
        self.assertEqual(est_8k.net_credits, 70)

        # Standard geo + standard tex: 30
        est_std = pricing.estimate_generate(
            tier="hd",
            hd_version="v3.5-20261108",
            texture=True,
            texture_quality="standard",
            pbr=False,
            geometry_quality="standard",
        )
        self.assertEqual(est_std.net_credits, 30)

        # Batch scales linearly: 2 * 60 = 120
        est_batch = pricing.estimate_generate(
            tier="hd",
            hd_version="v3.5-20261108",
            mode="batch",
            batch_count=2,
            texture=True,
            texture_quality="detailed",
            pbr=True,
            geometry_quality="detailed",
        )
        self.assertEqual(est_batch.net_credits, 120)

    def test_h35_rejects_quad_without_silent_override(self) -> None:
        with self.assertRaisesRegex(ValueError, "triangle"):
            pricing.estimate_generate(tier="hd", hd_version="v3.5-20261108", quad=True)
        args = driver.build_parser().parse_args(
            ["--region", "global", "generate", "--mode", "text", "--prompt", "statue", "--quad"]
        )
        self.assertEqual(args.hd_version, "v3.5-20261108")
        with self.assertRaisesRegex(ValueError, "triangle"):
            actions.validate_generate(args)

    def test_h31_and_smartmesh_p2_pricing_unchanged(self) -> None:
        h31 = pricing.estimate_generate(
            tier="hd",
            hd_version="v3.1-20260211",
            texture=True,
            texture_quality="detailed",
            pbr=False,
            geometry_quality="detailed",
        )
        self.assertEqual(h31.net_credits, 50)
        p2 = pricing.estimate_generate(tier="smartmesh", quad=True, variations=4)
        self.assertEqual(p2.net_credits, 100)
        p2_trial = pricing.estimate_generate(tier="smartmesh", quad=True, smart_mesh_p2_trial=1)
        self.assertEqual(p2_trial.net_credits, 0)
        self.assertEqual(p2_trial.trial_used, "smart_mesh_p2_trial")

    def test_parser_resolves_region_defaults(self) -> None:
        p = driver.build_parser()
        cn_gen = p.parse_args(["--region", "cn", "generate", "--mode", "text", "--prompt", "a"])
        self.assertEqual(cn_gen.hd_version, "v3.1-20260211")
        self.assertEqual(cn_gen.lp_version, "Nexus-v2.0-20260801")

        gl_gen = p.parse_args(["--region", "global", "generate", "--mode", "text", "--prompt", "a"])
        self.assertEqual(gl_gen.hd_version, "v3.5-20261108")

        cn_override = p.parse_args(
            ["--region", "cn", "generate", "--mode", "text", "--prompt", "a", "--hd-version", "h3.5"]
        )
        self.assertEqual(cn_override.hd_version, "v3.5-20261108")

        gl_rig = p.parse_args(
            ["--region", "global", "postprocess", "--operation", "rigging", "--project-id", "00000001-0000-4000-8000-000000000001"]
        )
        self.assertEqual(gl_rig.rig_version, "v3.0-20260909")

    def test_parse_submit_credits_handles_promo_and_trial_strikethrough(self) -> None:
        self.assertEqual(driver._parse_submit_credits("生成模型\n50"), 50)
        self.assertEqual(driver._parse_submit_credits("生成模型\n100\n0"), 0)
        self.assertEqual(driver._parse_submit_credits("Generate\n60\n130"), 60)
        self.assertIsNone(driver._parse_submit_credits("Generate"))

    def test_exact_price_accepts_h35_promo_with_credit_prop(self) -> None:
        self.assertEqual(
            actions.exact_price("Generate\n60\n130", 60, driver._parse_submit_credits, credit_prop=60),
            60,
        )
        with self.assertRaises(RuntimeError):
            actions.exact_price("Generate\n60\n130", 130, driver._parse_submit_credits, credit_prop=60)

    def test_version_option_labels_and_studio_ui_h35_selection(self) -> None:
        self.assertIn("H3.5", _version_option_labels("hd", "v3.5-20261108"))
        self.assertIn("v3.1", _version_option_labels("hd", "v3.1-20260211"))
        self.assertIn("P2.0", _version_option_labels("smartmesh", "Nexus-v2.0-20260801"))

        state = {"hdVersion": "v3.1-20260211", "tab": "high_detail"}

        def fake_call(op: str, *a: str) -> str:
            if op == "eval":
                return json.dumps({"state": dict(state), "controls": [], "dialogs": []})
            if op == "find":
                css = a[1]
                if css == 'button[role="combobox"]':
                    return json.dumps({"entries": [
                        {"ref": "1", "visible": True, "text": "Public"},
                        {"ref": "2", "visible": True, "text": "H3.1 – Highest Quality"},
                    ]})
                if css == '[role="option"]':
                    return json.dumps({"entries": [
                        {"ref": "10", "visible": True, "text": "H3.5 Preview New True 3D Fidelity"},
                        {"ref": "11", "visible": True, "text": "H3.1 Highest Quality"},
                    ]})
            if op == "click":
                if a[0] == "10":
                    state["hdVersion"] = "v3.5-20261108"
                return json.dumps({"clicked": True, "match_level": "exact", "click_method": "cdp", "hit": True})
            raise AssertionError(f"unexpected call: {op} {a}")

        ui = StudioUI(fake_call, json.loads)
        ui.set_version("hd", "v3.5-20261108")
        self.assertEqual(state["hdVersion"], "v3.5-20261108")


if __name__ == "__main__":
    unittest.main(verbosity=2)
