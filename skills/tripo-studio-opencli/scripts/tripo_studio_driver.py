#!/usr/bin/env python3
"""
Tripo Studio Web (studio.tripo3d.com) orchestrator driven via OpenCLI Browser Bridge
with strict ledger reservation, dual credit verification, zero auto-retry on spend,
and SHA256-verified export download into the explicitly configured runtime directory..
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import re
from pathlib import Path
from typing import Any

from tripo_pricing import estimate_generate, estimate_postprocess

DEFAULT_RUNTIME_DIR = Path(os.environ.get("TRIPO_STUDIO_RUNTIME_DIR", str(Path.cwd() / ".tripo-studio")))
LEDGER_SCRIPT = Path(__file__).resolve().parent / "tripo_ledger.py"

TERMINAL_FAILURE_STATUSES = {"failed", "expired", "cancelled", "banned", "unknown"}
USE_RESOURCE_BARRIER = True

class OperatorFailed(RuntimeError):
    """A matching progress receipt positively reports terminal failure."""

class OpenCLICommandError(RuntimeError):
    def __init__(self,message,code=None):
        super().__init__(message);self.code=code

REGION_ORIGINS = {
    "cn": "https://studio.tripo3d.com",
    "global": "https://studio.tripo3d.ai",
}

MODE_TAB_INDEX = {
    "single": 0,
    "multiview": 1,
    "batch": 2,
    "text": 3,
}

POSTPROCESS_CONFIG: dict[str, dict[str, str]] = {
    "segmentation": {
        "route": "/workspace/segmentation",
        "trace": "studio_cn.workspace_segmentation.segment_model_button",
        "endpoint_hint": "/v2/studio/operation/ai_segmentation",
    },
    "fill-parts": {
        "route": "/workspace/fill-parts",
        "trace": "studio_cn.workspace_fill-parts.fill_part_button",
        "endpoint_hint": "/v2/studio/operation/ai_completion",
    },
    "retopology": {
        "route": "/workspace/retopology",
        "trace": "studio_cn.workspace_retopology.retopology_button",
        "endpoint_hint": "/v2/studio/operation/remesh",
    },
    "smart-uv": {
        "route": "/workspace/smart-uv",
        "trace": "",
        "endpoint_hint": "/v2/studio/operation/uv_edit/generate",
    },
    "texture": {
        "route": "/workspace/texture",
        "trace": "studio_cn.workspace_texture.generate_texture_button",
        "endpoint_hint": "/v2/studio/operation/texture_model",
    },
    "texture-edit": {
        "route": "/workspace/texture-edit",
        "trace": "studio_cn.workspace_texture-edit.generate_texture_preview_button",
        "endpoint_hint": "/v2/studio/operation/retexture_generate",
    },
    "texture-pbr": {
        "route": "/workspace/texture-pbr",
        "trace": "studio_cn.workspace_texture-pbr.generate_pbr_button",
        "endpoint_hint": "/v2/studio/operation/pbr_generate",
    },
    "texture-upscale": {
        "route": "/workspace/texture-upscale",
        "trace": "studio_cn.workspace_texture-upscale.texture_upscale_button",
        "endpoint_hint": "/v2/studio/operation/texture_upscaler",
    },
    "rigging": {
        "route": "/workspace/rigging",
        "trace": "studio_cn.workspace_rigging.rig_button",
        "endpoint_hint": "/v2/studio/operation/rigging_model",
    },
}


def _run_cmd(cmd: list[str], *, check: bool = True, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if check and proc.returncode != 0:
        # Adapter output can contain signed model URLs. Never echo raw args or bodies.
        message=re.sub(r'https?://[^\s<>\"\x27]+','<redacted-url>',(proc.stderr or proc.stdout)[-1200:])
        message=re.sub(r'(?i)(bearer\s+)[A-Za-z0-9._-]+',r'\1<redacted>',message)
        match=re.search(r'\[([a-z][a-z0-9_]*)\]',message)
        code=match.group(1) if match else None
        try:
            envelope=_extract_json(proc.stdout)
            if isinstance(envelope,dict) and isinstance(envelope.get('error'),dict):
                found=envelope['error'].get('code')
                if isinstance(found,str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,64}',found):code=found
        except (ValueError,TypeError):pass
        raise OpenCLICommandError(f"Tool command exited {proc.returncode}; no automatic retry. {message}",code)
    return proc


def _resolve_opencli_bin() -> str:
    """Resolve full path to opencli binary/shim (needed on Windows for opencli.cmd)."""
    found = shutil.which("opencli")
    if found:
        return found
    appdata = os.environ.get("APPDATA")
    if appdata:
        cand = Path(appdata) / "npm" / "opencli.cmd"
        if cand.is_file():
            return str(cand)
    return "opencli"


OPENCLI_BIN = _resolve_opencli_bin()


def _resolve_opencli_command() -> list[str]:
    """Run the installed npm CLI without CMD reparsing JS/JSON arguments.

    Python's Windows argument quoting targets CreateProcess, not CMD's `%*`.
    The npm .cmd shim can strip quotes/newlines from eval scripts and selectors.
    Resolve the same installed package's declared bin; do not install or upgrade.
    """
    binary = Path(OPENCLI_BIN)
    if os.name != "nt" or binary.suffix.lower() not in {".cmd", ".bat", ".ps1"}:
        return [OPENCLI_BIN]
    package = binary.parent / "node_modules" / "@jackwener" / "opencli"
    metadata = json.loads((package / "package.json").read_text(encoding="utf-8"))
    declared = metadata.get("bin", {})
    entry_name = declared.get("opencli") if isinstance(declared, dict) else declared
    if not isinstance(entry_name, str):
        raise RuntimeError("Installed OpenCLI package has no declared executable.")
    entry = (package / entry_name).resolve()
    if package.resolve() not in entry.parents or not entry.is_file():
        raise RuntimeError("Installed OpenCLI executable is missing or outside its package.")
    node = shutil.which("node")
    if not node:
        raise RuntimeError("Node runtime missing; no CMD fallback for UI script arguments.")
    return [node, str(entry)]


OPENCLI_COMMAND = _resolve_opencli_command()
_BROWSER_PAGES: dict[tuple[str, str], str] = {}
_PAGED_BROWSER_COMMANDS = {
    "state", "eval", "click", "focus", "keys", "upload", "wait", "find",
    "get", "type", "fill", "select", "check", "uncheck", "screenshot",
    "network", "console", "scroll", "hover",
}


def _opencli(profile: str, *args: str, check: bool = True, timeout: int = 120) -> str:
    # A successful CDP response is not proof that a hidden/stale tab changed.
    # Keep UI operations visible and pin every follow-up to the opened page.
    # Read adapters must not release the shared Studio lease while an unsaved
    # UV/texture preview is being inspected. This is a per-call CLI contract.
    if args and args[0]=='tripo-studio' and '--site-session' not in args:
        args=(*args,'--site-session','persistent')
    browser_call = len(args) >= 3 and args[0] == "browser"
    session_key = (profile, args[1]) if browser_call else None
    operation = args[2] if browser_call else None
    if operation == "open":
        if session_key in _BROWSER_PAGES:raise RuntimeError('Owned session already has a page; close it before opening another.')
        # `open` may reuse a leased tab. Allocate our own tab before navigation.
        created=_extract_json(_run_cmd([*OPENCLI_COMMAND,'--profile',profile,'browser',args[1],'tab','new','--window','foreground']).stdout)
        owned=created.get('page')
        if not isinstance(owned,str) or not owned:raise RuntimeError('No unique owned tab returned.')
        _BROWSER_PAGES[session_key]=owned
        args=(*args,'--tab',owned)
        if "--window" not in args:
            args = (*args, "--window", "foreground")
    elif operation=='close':
        owned=_BROWSER_PAGES.get(session_key)
        if owned:
            closed=_run_cmd([*OPENCLI_COMMAND,'--profile',profile,'browser',args[1],'tab','close',owned],check=check,timeout=timeout)
            if closed.returncode==0:_BROWSER_PAGES.pop(session_key,None)
            return closed.stdout
        return json.dumps({'closed':False,'reason':'no owned page'})
    elif operation in _PAGED_BROWSER_COMMANDS and "--tab" not in args:
        page = _BROWSER_PAGES.get(session_key)
        if not page:
            raise RuntimeError("Open the owned browser session before a UI command; no unpinned fallback.")
        args = (*args, "--tab", page)
    cmd = [*OPENCLI_COMMAND, "--profile", profile, *args]
    output = _run_cmd(cmd, check=check, timeout=timeout).stdout
    if operation == "open":
        opened = _extract_json(output)
        page = opened.get("page") if isinstance(opened, dict) else None
        if not isinstance(page, str) or not page.strip():
            raise RuntimeError("OpenCLI did not return an owned page identity; abort UI operations.")
        if page != owned:
            raise RuntimeError('Navigation changed the owned page identity; no UI submission is allowed.')
        _BROWSER_PAGES[session_key] = page
    elif operation == "close":
        _BROWSER_PAGES.pop(session_key, None)
    return output


def _extract_json(text: str) -> Any:
    """Parse JSON ignoring trailing CLI update banners or experimental warnings."""
    s = text.strip()
    start_arr = s.find("[")
    start_obj = s.find("{")
    candidates = [idx for idx in (start_arr, start_obj) if idx >= 0]
    if not candidates:
        raise ValueError(f"No JSON found in output:\n{s}")
    start = min(candidates)
    decoder = json.JSONDecoder()
    obj, _ = decoder.raw_decode(s[start:])
    return obj


def _ledger(ledger_path: Path, *args: str) -> dict[str, Any]:
    proc = _run_cmd([sys.executable, str(LEDGER_SCRIPT), "--ledger", str(ledger_path), *args], check=True)
    return _extract_json(proc.stdout)


def _ensure_daemon(profile: str) -> None:
    if not profile:
        raise ValueError("Set --profile or TRIPO_STUDIO_PROFILE to your existing Browser Bridge profile.")
    probe = _run_cmd([*OPENCLI_COMMAND, "--profile", profile, "doctor"], check=False, timeout=15)
    if probe.returncode == 0 and "[OK] Extension: connected" in probe.stdout:
        return
    raise RuntimeError('OpenCLI Browser Bridge is not connected. Inspect doctor; no automatic service restart.')


def _parse_submit_credits(btn_text: str) -> int | None:
    """Extract live credit integer from submit button text (e.g. '生成模型 50' or '生成模型 100 0')."""
    tokens = [int(tok) for tok in btn_text.replace("\n", " ").split() if tok.isdigit()]
    if not tokens:
        return None
    # When trial strikethrough is shown (e.g. '生成模型 100 0'), the final digit is the effective price
    return tokens[-1]


def _poll_operator(profile: str, region: str, operator_id: str, timeout_sec: int) -> dict[str, Any]:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        raw = _opencli(profile, "tripo-studio", "progress", "--ids", operator_id, "--region", region, "-f", "json")
        rows = _extract_json(raw)
        if isinstance(rows, list) and rows:
            row = next((r for r in rows if str(r.get('operator_id',''))==operator_id),None)
            if row is None:raise RuntimeError('Progress response did not match the requested operator.')
            status = str(row.get("status", "")).lower()
            if status == "success":
                return {k:row.get(k) for k in ('operator_id','status','progress','error_code') if k in row}
            if status in TERMINAL_FAILURE_STATUSES:
                if status=='unknown':raise RuntimeError('Operator state is unknown; no automatic retry.')
                raise OperatorFailed(
                    f"Operator {operator_id} ended with terminal status='{status}' "
                    f"(error_code={row.get('error_code')}). NO auto-retry."
                )
        time.sleep(4.0)
    raise TimeoutError(f"Timed out polling operator {operator_id} after {timeout_sec}s; treat as unknown and halt.")


def _fetch_preflight(profile: str, region: str) -> dict[str, Any]:
    bal = _extract_json(_opencli(profile, "tripo-studio", "balance", "--region", region, "-f", "json"))[0]
    quota = _extract_json(_opencli(profile, "tripo-studio", "quota", "--region", region, "-f", "json"))[0]
    return {"balance": bal, "quota": quota}


def cmd_preflight(args: argparse.Namespace) -> dict[str, Any]:
    _ensure_daemon(args.profile)
    data = _fetch_preflight(args.profile, args.region)
    ledger_path = Path(args.ledger)
    if args.task_id and ledger_path.exists():
        try:
            data["ledger"] = _ledger(ledger_path, "status", "--task-id", args.task_id)
        except Exception as exc:
            data["ledger_note"] = str(exc)
    return {"ok": True, "preflight": data}


def cmd_generate(args):
    import tripo_actions
    from tripo_ledger import _ledger_lock
    with _ledger_lock(Path(args.runtime_dir)/'studio-browser.lock'):
        return tripo_actions.generate(sys.modules[__name__],args)


def cmd_postprocess(args):
    import tripo_actions
    from tripo_ledger import _ledger_lock
    with _ledger_lock(Path(args.runtime_dir)/'studio-browser.lock'):
        return tripo_actions.postprocess(sys.modules[__name__],args)


def cmd_export(args):
    import tripo_export
    from tripo_ledger import _ledger_lock
    # Every export/resume/status for the same record is serialized. Download
    # status itself reads the downloader and ledger only; it must not wait for
    # an unrelated cloud generation to release the browser for many minutes.
    identity=json.dumps([args.task_id,args.step_key],separators=(',',':')).encode()
    record_key=hashlib.sha256(identity).hexdigest()[:24]
    with _ledger_lock(Path(args.runtime_dir)/('export-record-'+record_key+'.lock')):
        if args.download_job:
            return tripo_export.export(sys.modules[__name__],args)
        with _ledger_lock(Path(args.runtime_dir)/'studio-browser.lock'):
            return tripo_export.export(sys.modules[__name__],args)


class StudioArgumentParser(argparse.ArgumentParser):
    def parse_args(self,args=None,namespace=None):
        parsed=super().parse_args(args,namespace)
        if hasattr(parsed,'runtime_dir') and getattr(parsed,'ledger',None) is None:
            parsed.ledger=str(Path(parsed.runtime_dir)/'ledger.json')
        return parsed


def build_parser() -> argparse.ArgumentParser:
    p = StudioArgumentParser(description="Tripo Studio Web OpenCLI Orchestrator")
    p.add_argument("--profile", default=os.environ.get("TRIPO_STUDIO_PROFILE", ""), help="Connected Chrome Browser Bridge profile alias/ID")
    p.add_argument("--region", default="cn", choices=["cn", "global"])
    p.add_argument("--runtime-dir", default=str(DEFAULT_RUNTIME_DIR))
    p.add_argument("--ledger", default=None, help="Ledger path; defaults to <runtime-dir>/ledger.json")
    sub = p.add_subparsers(dest="subcmd", required=True)

    p_pre = sub.add_parser("preflight", help="Check Studio balance, quota, and ledger status (0 spend)")
    p_pre.add_argument("--task-id", default="")

    p_gen = sub.add_parser("generate", help="Run dry-run estimate or execute Tripo Studio generation")
    p_gen.add_argument("--mode", required=True, choices=["single", "multiview", "batch", "text"])
    p_gen.add_argument("--tier", default="hd", choices=["hd", "smartmesh"])
    p_gen.add_argument("--hd-version", default="v3.1-20260211")
    p_gen.add_argument("--lp-version", default="Nexus-v2.0-20260801")
    p_gen.add_argument("--quad", action="store_true")
    p_gen.add_argument("--face-limit", type=int, default=0)
    p_gen.add_argument("--variations", type=int, default=1, choices=[1, 2, 4])
    p_gen.add_argument("--generate-parts", action="store_true")
    p_gen.add_argument("--smart-poly", action="store_true")
    p_gen.add_argument("--no-texture", action="store_true")
    p_gen.add_argument("--pbr", action="store_true")
    p_gen.add_argument("--texture-quality", default="detailed", choices=["standard", "detailed", "extreme"])
    p_gen.add_argument("--geometry-quality", default="detailed", choices=["standard", "detailed"])
    p_gen.add_argument("--visibility", default="shareable", choices=["shareable", "private", "public"])
    p_gen.add_argument("--images", nargs="*", default=[])
    p_gen.add_argument("--front", default="")
    p_gen.add_argument("--left", default="")
    p_gen.add_argument("--right", default="")
    p_gen.add_argument("--back", default="")
    p_gen.add_argument("--prompt", default="")
    p_gen.add_argument("--t-pose", action="store_true")
    p_gen.add_argument("--task-id", default="")
    p_gen.add_argument("--step-key", default="")
    p_gen.add_argument("--timeout-sec", type=int, default=600)
    p_gen.add_argument("--execute-spend", action="store_true")
    p_gen.add_argument('--prepare-only',action='store_true',help='Configure and verify real UI; never submit')
    p_gen.add_argument('--prepare-upload',action='store_true',help='Explicitly upload input images during --prepare-only')

    p_post = sub.add_parser("postprocess", help="Drive post-processing modules on an existing project_id")
    p_post.add_argument("--operation", required=True, choices=list(POSTPROCESS_CONFIG.keys()))
    p_post.add_argument("--project-id", required=True)
    p_post.add_argument("--triangle", action="store_true")
    p_post.add_argument("--smart-poly", action="store_true")
    p_post.add_argument("--texture-quality", default="standard", choices=["standard", "detailed", "extreme"])
    p_post.add_argument("--ref-image", default="")
    p_post.add_argument("--upscale-extreme", action="store_true")
    p_post.add_argument('--skip-upscaler-trial',action='store_true',help='Explicit paid 4K estimate when the live UI does not honor the reported trial; exact live price and approved budget still required')
    p_post.add_argument("--prompt", default="", help="Texture: select text input and fill its prompt; texture-edit: prompt generation mode")
    p_post.add_argument("--rig-type", default="biped", choices=["biped", "animal"])
    p_post.add_argument("--brush-mode", action="store_true")
    p_post.add_argument("--task-id", default="")
    p_post.add_argument("--step-key", default="")
    p_post.add_argument("--timeout-sec", type=int, default=600)
    p_post.add_argument("--execute-spend", action="store_true")
    p_post.add_argument('--prepare-only',action='store_true',help='Verify postprocess UI without submitting')
    p_post.add_argument('--prepare-upload',action='store_true')
    p_post.add_argument('--face-limit',type=int,default=10000)
    p_post.add_argument('--granularity',choices=['coarse','medium','fine'],default='medium')
    p_post.add_argument('--apply-preview-step',default='',help='Apply the exact texture-edit preview recorded by this task/step; never regenerate it')
    p_post.add_argument('--unpriced-apply-cap',type=int,default=None,help='Local reservation for an explicitly authorized unpriced UV save, not a provider charge cap. Automatic texture-preview save is blocked pending a verified price contract.')
    p_post.add_argument('--uv-retry',action='store_true',help='Explicitly request a next UV candidate; no automatic retry on failure/unknown')
    p_post.add_argument('--animation',default='',help='Rigging: choose an exact visible animation card label, using a separate budgeted step')
    p_post.add_argument('--retarget-credit-cap',type=int,default=None,help='Local reserved exposure for an explicitly authorized unpriced retarget card; not a provider charge cap and never presumed free')

    p_exp = sub.add_parser("export", help="Export artifact via opencli export-url and archive with SHA256")
    p_exp.add_argument("--task-id", required=True)
    p_exp.add_argument("--project-id", required=True)
    p_exp.add_argument("--model-format", default="gltf", choices=["gltf", "fbx", "obj", "stl", "usdz", "3mf"])
    p_exp.add_argument("--fbx-preset", default="blender", choices=["blender", "3dsmax", "mixamo"])
    p_exp.add_argument("--texture-packaging", default="embedded", choices=["embedded", "zip"])
    p_exp.add_argument("--texture-size", type=int, default=2048)
    p_exp.add_argument("--name", default="")
    p_exp.add_argument("--with-animation", action="store_true")
    p_exp.add_argument("--enable-bake-animation", action="store_true", help="Legacy flag: current-pose STATIC baking, not Action baking; new exports rejected until pose/frame mapping is verified")
    p_exp.add_argument('--animation-names',nargs='*',default=[],help='Exact completed retarget names to include with --with-animation')
    p_exp.add_argument('--pivot-to-center-bottom',action='store_true')
    p_exp.add_argument('--pack-uv',action='store_true')
    p_exp.add_argument("--allow-nonowner-spend", action="store_true")
    p_exp.add_argument('--step-key',default='')
    p_exp.add_argument('--execute-spend',action='store_true',help='Explicitly execute the budgeted export once')
    p_exp.add_argument('--download-job',default='',help='Check one existing download job; never re-export')
    p_exp.add_argument('--resume-export',action='store_true',help='Resolve a previously recorded export operator; never create another export')

    return p


def main() -> int:
    # Match subprocess consumers' explicit UTF-8 decoding on every Windows locale.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args()
    if getattr(args,'prepare_only',False) and getattr(args,'execute_spend',False):parser.error('--prepare-only and --execute-spend are mutually exclusive')
    if getattr(args,'prepare_upload',False) and not getattr(args,'prepare_only',False):parser.error('--prepare-upload requires --prepare-only')
    dispatch = {
        "preflight": cmd_preflight,
        "generate": cmd_generate,
        "postprocess": cmd_postprocess,
        "export": cmd_export,
    }
    try:
        res = dispatch[args.subcmd](args)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
