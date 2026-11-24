"""Inspect or save a received transfer from a specifically named Windows receiver."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
from uuid import UUID

from tripo_ledger import _save_ledger_atomic


BRIDGE_PORT = 60600
LOOPBACK = '127.0.0.1'
RECEIVER_VERSION = (5, 3, 0)
RECEIVER_HOST = 'Windows Blender ' + '.'.join(map(str, RECEIVER_VERSION))
RECEIVER_BUILD = '02f0899d78e3'
REPLY_LIMIT = 16384
SAVE_TIMEOUT = 120
SAVE_REPLY_LIMIT = 256 * 1024


def _blend_path(value):
    path = Path(value)
    if not path.is_absolute() or path.suffix.lower() != '.blend':
        raise ValueError('Receiver and checkpoint paths must be absolute .blend paths.')
    path = path.resolve(strict=True)
    if not path.is_file() or path.suffix.lower() != '.blend':
        raise ValueError('Receiver and checkpoint must point to regular .blend files.')
    return path


def prepare_binding(binding):
    """Normalize the user's target and confirm it has a separate saved recovery copy."""
    pid, port = binding['pid'], binding['mcp_port']
    if type(pid) is not int or not 0 < pid < 2**31:
        raise ValueError('An explicit valid Blender process ID is required.')
    if type(port) is not int or not 0 < port <= 65535 or port == BRIDGE_PORT:
        raise ValueError('An explicit Blender Lab port distinct from 60600 is required.')
    scene = binding['scene']
    if not isinstance(scene, str) or not scene.strip():
        raise ValueError('An explicit target Blender scene is required.')
    work_file = _blend_path(binding['work_file'])
    checkpoint = _blend_path(binding['checkpoint'])
    if work_file.samefile(checkpoint):
        raise ValueError('The receiver checkpoint must be a separate file.')
    return {'pid': pid, 'mcp_port': port, 'scene': scene,
            'work_file': str(work_file), 'checkpoint': str(checkpoint)}


def _file_hash(path):
    with Path(path).open('rb') as file:
        return hashlib.file_digest(file, 'sha256').hexdigest()


def _checkpoint_hash(binding):
    work_hash = _file_hash(binding['work_file'])
    if _file_hash(binding['checkpoint']) != work_hash:
        raise RuntimeError('The receiver checkpoint differs from the saved working file.')
    return work_hash


def _listener_owner(pid, port):
    if sys.platform != 'win32':
        raise RuntimeError('The verified Tripo receiver requires Windows.')
    # Query OS ownership without opening the Tripo socket. Its receiver would
    # disconnect the official client if a WebSocket diagnostic connected there.
    powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    query = f"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$ports = @({BRIDGE_PORT}, {port})
$listeners = @(Get-NetTCPConnection -State Listen | Where-Object {{ $_.LocalPort -in $ports }})
$process = Get-Process -Id {pid}
[pscustomobject]@{{
    listeners = @($listeners | Select-Object LocalPort, LocalAddress, OwningProcess)
    binary = $process.Path
    started = $process.StartTime.ToUniversalTime().Ticks.ToString()
}} | ConvertTo-Json -Compress -Depth 4
"""
    try:
        reply = subprocess.run(
            [str(powershell), '-NoProfile', '-NonInteractive', '-Command', query],
            capture_output=True, timeout=8, check=True, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        result = json.loads(reply.stdout.decode('utf-8-sig'))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise RuntimeError('Could not confirm Windows listener ownership for the named Blender PID.') from exc
    return _validate_owner(result, pid, port)


def _validate_owner(result, pid, port):
    if not result.get('binary') or Path(result['binary']).name.lower() != 'blender.exe':
        raise RuntimeError('The named receiver process is not Blender.')
    for target_port in (BRIDGE_PORT, port):
        listeners = [row for row in result['listeners'] if row['LocalPort'] == target_port]
        if len(listeners) != 1 or listeners[0]['OwningProcess'] != pid or listeners[0]['LocalAddress'] != LOOPBACK:
            raise RuntimeError(f'Port {target_port} must have one loopback listener owned by the named Blender PID.')
    return {'binary': result['binary'], 'started': result['started']}


# These fixed probes use the Blender Lab 1.0.2 execute contract. The caller
# supplies only data, and Blender verifies its own PID before accessing the scene.
_RECEIVER_CONTEXT = (
    f"RECEIVER_VERSION = {RECEIVER_VERSION!r}\n"
    f"RECEIVER_HOST = {RECEIVER_HOST!r}\n"
    f"RECEIVER_BUILD = {RECEIVER_BUILD!r}\n"
) + """
import bpy
import hashlib
import os
from pathlib import Path
import sys

def receiver_context(binding, *, require_clean=False):
    if os.getpid() != binding['pid']:
        raise RuntimeError('The Blender Lab process no longer matches the selected PID.')
    if (sys.platform != 'win32' or bpy.app.background or tuple(bpy.app.version) != RECEIVER_VERSION
            or bpy.app.build_hash != RECEIVER_BUILD.encode('ascii')):
        raise RuntimeError(f'The receiver requires interactive {RECEIVER_HOST} build {RECEIVER_BUILD}.')
    binary = os.path.normcase(str(Path(bpy.app.binary_path).resolve()))
    if binary != binding['binary']:
        raise RuntimeError('The Blender binary differs from the OS listener owner.')
    work_file = os.path.normcase(str(Path(bpy.data.filepath).resolve()))
    if not bpy.data.is_saved or work_file != binding['work_file'] or (require_clean and bpy.data.is_dirty):
        raise RuntimeError('Open the selected saved working file and save any pending edits before transfer.')
    scene = bpy.context.scene
    window = bpy.context.window
    if (window is None or scene is None or window.scene != scene or bpy.context.mode != 'OBJECT'
            or scene.name != binding['scene'] or len(bpy.data.scenes) != 1):
        raise RuntimeError('The exact saved, empty receiving scene must be active in Object Mode.')
    addon = sys.modules.get('Tripo3d_Blender_Bridge')
    task = sys.modules.get('Tripo3d_Blender_Bridge.core.task')
    if addon is None or tuple(addon.bl_info.get('version', ())) != (1, 1, 0) or task is None:
        raise RuntimeError('The verified Tripo Bridge 1.1.0 receiver is unavailable.')
    manager = task.task_manager
    receiver = getattr(manager, 'ws_server', None)
    thread = getattr(manager, 'ws_thread', None)
    loop = getattr(receiver, 'loop', None)
    if (receiver is None or thread is None or not thread.is_alive() or loop is None or not loop.is_running()
            or receiver.stop_event.is_set() or receiver.host != '127.0.0.1' or receiver.port != 60600):
        raise RuntimeError('The official Tripo Bridge receiver is unavailable on its verified port.')
    snapshot = receiver.transfer_snapshot()
    connection_ids = sorted(receiver._sockets.copy())
    pending = receiver.pending_import_count()
    if (len(connection_ids) != 1 or pending != 0
            or snapshot.get('phase') not in ('idle', 'complete') or snapshot.get('error')
            or snapshot.get('percent') != (0 if snapshot.get('phase') == 'idle' else 100)
            or type(snapshot.get('revision')) is not int or snapshot['revision'] < 0):
        raise RuntimeError('The receiver needs one connected Studio client and no active or failed import.')
    connection_fingerprint = hashlib.sha256(connection_ids[0].encode('utf-8')).hexdigest()
    identity = {'pid': os.getpid(), 'receiver_host': RECEIVER_HOST, 'receiver_build': RECEIVER_BUILD,
            'scene_uid': scene.session_uid,
            'receiver_instance': id(receiver), 'connection_fingerprint': connection_fingerprint,
            'transfer_revision': snapshot['revision'], 'transfer_phase': snapshot['phase']}
    return scene, identity, snapshot, pending
"""

_LIVE_PROBE = _RECEIVER_CONTEXT + """
def inspect_target(binding, nonce):
    try:
        _, identity, _, _ = receiver_context(binding, require_clean=True)
        if len(bpy.data.objects) != 0:
            raise RuntimeError('The exact saved, empty receiving scene must be active in Object Mode.')
        return {'nonce': nonce, **identity}
    except RuntimeError as error:
        return {'nonce': nonce, 'error': str(error)}
"""

_SAVE_PROBE = _RECEIVER_CONTEXT + """
def save_target(binding, nonce):
    try:
        scene, identity, snapshot, pending = receiver_context(binding)
        if any(identity[key] != value for key, value in binding['expected_identity'].items()):
            raise RuntimeError('The Blender scene, receiver instance or Studio connection changed.')
        if snapshot['phase'] != 'complete' or snapshot['file_id'] != binding['project_id']:
            raise RuntimeError('The receiver has no completed import for the selected Studio project.')
        # This exact 1.1.0 implementation advances once per new transfer or
        # unrelated failure. Multiple revisions make an automatic match ambiguous.
        if snapshot['revision'] != binding['expected_revision']:
            raise RuntimeError('The receiver must show exactly one new transfer since the pre-send check.')
        if not scene.objects or set(scene.objects) != set(bpy.data.objects):
            raise RuntimeError('The selected scene must contain the entire received object set.')
        objects = {}
        has_material = False
        for obj in scene.objects:
            info = {'type': obj.type}
            if obj.type == 'MESH':
                materials = [slot.material.name if slot.material else None for slot in obj.material_slots]
                info.update(vertices=len(obj.data.vertices), faces=len(obj.data.polygons), materials=materials)
                has_material |= info['vertices'] > 0 and info['faces'] > 0 and any(materials)
            objects[obj.name] = info
        if not has_material:
            raise RuntimeError('The received scene must contain a nonempty mesh with a material.')
        path = Path(binding['destination'])
        if path.exists():
            raise RuntimeError('The staged work copy already exists.')
        status = bpy.ops.wm.save_as_mainfile(filepath=str(path), copy=True)
        if status != {'FINISHED'} or not path.is_file():
            raise RuntimeError('Blender did not save the received work copy.')
        _, final_identity, final_snapshot, final_pending = receiver_context(binding)
        if final_identity != identity or final_snapshot != snapshot or final_pending != pending:
            raise RuntimeError('The receiver changed during the save. Verify the transfer before continuing.')
        return {'nonce': nonce, 'pid': os.getpid(), 'receiver_host': identity['receiver_host'],
                'receiver_build': identity['receiver_build'], 'target_scene': scene.name, 'added_objects': objects,
                'receiver': {key: snapshot[key] for key in ('phase', 'percent', 'file_id', 'revision')},
                'pending_imports': pending}
    except RuntimeError as error:
        return {'nonce': nonce, 'error': str(error)}
"""


def _run_blender(port, code, *, timeout, reply_limit):
    payload = json.dumps({'type': 'execute', 'strict_json': True, 'code': code}).encode('utf-8') + b'\0'
    received = bytearray()
    deadline = time.monotonic() + timeout
    try:
        with socket.create_connection((LOOPBACK, port), timeout=4) as client:
            client.sendall(payload)
            while b'\0' not in received and len(received) < reply_limit:
                client.settimeout(max(0.01, deadline - time.monotonic()))
                chunk = client.recv(min(4096, reply_limit - len(received)))
                if not chunk:
                    break
                received.extend(chunk)
        payload, delimiter, trailing = received.partition(b'\0')
        if not delimiter or trailing:
            raise ValueError('Invalid Blender Lab response framing')
        response = json.loads(payload)
    except (OSError, ValueError) as exc:
        raise RuntimeError('The named Blender Lab listener did not return a complete live check.') from exc
    if not isinstance(response, dict) or response.get('status') != 'ok' or not isinstance(response.get('result'), dict):
        raise RuntimeError('The named Blender Lab listener could not inspect its receiver.')
    return response['result']


def _inspect_blender(port, parameters, nonce):
    code = _LIVE_PROBE + f'\nresult = inspect_target({parameters!r}, {nonce!r})\n'
    return _run_blender(port, code, timeout=8, reply_limit=REPLY_LIMIT)


def _probe_parameters(binding, owner):
    return {
        'pid': binding['pid'], 'scene': binding['scene'],
        'work_file': os.path.normcase(binding['work_file']),
        'binary': os.path.normcase(str(Path(owner['binary']).resolve())),
    }


def _check_probe_reply(binding, work_hash, owner, live, nonce):
    if live.get('nonce') != nonce:
        raise RuntimeError('The receiver did not answer this live challenge.')
    if 'error' in live:
        raise RuntimeError(live['error'])
    if live.get('receiver_host') != RECEIVER_HOST or live.get('receiver_build') != RECEIVER_BUILD:
        raise RuntimeError('The receiver did not report the required Blender host and build.')
    if live.get('pid') != binding['pid'] or _listener_owner(binding['pid'], binding['mcp_port']) != owner:
        raise RuntimeError('The known listener owner changed during the live check.')
    if _checkpoint_hash(binding) != work_hash:
        raise RuntimeError('The working file or checkpoint changed during the live check.')


def inspect_receiver(binding, *, baseline=None):
    """Obtain a fresh admission check. A baseline requires the same live state."""
    binding = prepare_binding(binding)
    work_hash = _checkpoint_hash(binding)
    pid, port = binding['pid'], binding['mcp_port']
    owner = _listener_owner(pid, port)
    nonce = secrets.token_hex(16)
    live = _inspect_blender(port, _probe_parameters(binding, owner), nonce)
    _check_probe_reply(binding, work_hash, owner, live, nonce)
    live_keys = ('receiver_host', 'receiver_build', 'scene_uid', 'receiver_instance',
                 'connection_fingerprint', 'transfer_revision', 'transfer_phase')
    current = {'ok': True, 'state': 'receiver_ready', **binding, 'snapshot_file_sha256': work_hash,
               'listener_started': owner['started'], **{key: live[key] for key in live_keys}}
    if baseline is not None and current != baseline:
        raise RuntimeError('The authorized receiver session, scene or file changed since initial inspection.')
    return current


def save_received(binding, baseline, project_id, export_operator_id, stage_dir):
    """Save a completed verified import as a new candidate in an empty stage."""
    for field, identity in (('Studio project', project_id), ('export operator', export_operator_id)):
        if not isinstance(identity, str) or str(UUID(identity)) != identity:
            raise ValueError(f'The {field} must be a canonical UUID.')
    binding = prepare_binding(binding)
    stage = Path(stage_dir).resolve(strict=True)
    if not stage.is_dir() or any(stage.iterdir()):
        raise ValueError('The receipt stage must be an existing empty directory.')
    work_hash = _checkpoint_hash(binding)
    if (baseline.get('ok') is not True or baseline.get('state') != 'receiver_ready'
            or any(baseline.get(key) != value for key, value in binding.items())
            or baseline.get('snapshot_file_sha256') != work_hash):
        raise RuntimeError('The pre-send receiver, working file or checkpoint no longer matches.')
    if baseline.get('receiver_host') != RECEIVER_HOST or baseline.get('receiver_build') != RECEIVER_BUILD:
        raise RuntimeError('The original receiver baseline belongs to a different or unrecorded host. Use its original records.')
    revision = baseline.get('transfer_revision')
    if type(revision) is not int or revision < 0:
        raise ValueError('The pre-send transfer revision is required.')
    owner = _listener_owner(binding['pid'], binding['mcp_port'])
    if owner['started'] != baseline.get('listener_started'):
        raise RuntimeError('The named Blender process restarted since the pre-send check.')
    identity_keys = ('scene_uid', 'receiver_instance', 'connection_fingerprint', 'receiver_host', 'receiver_build')
    identity = {key: baseline.get(key) for key in identity_keys}
    if (any(type(identity[key]) is not int for key in identity_keys[:2])
            or not isinstance(identity['connection_fingerprint'], str)
            or len(identity['connection_fingerprint']) != 64):
        raise ValueError('The pre-send Blender scene, receiver and connection identities are required.')
    destination = stage / 'received.blend'
    parameters = {
        **_probe_parameters(binding, owner),
        'expected_identity': identity,
        'expected_revision': revision + 1,
        'project_id': project_id,
        'destination': str(destination),
    }
    nonce = secrets.token_hex(16)
    code = _SAVE_PROBE + f'\nresult = save_target({parameters!r}, {nonce!r})\n'
    live = _run_blender(binding['mcp_port'], code, timeout=SAVE_TIMEOUT, reply_limit=SAVE_REPLY_LIMIT)
    _check_probe_reply(binding, work_hash, owner, live, nonce)
    result = {
        'ok': True, 'state': 'receiver_saved', 'blender_pid': binding['pid'],
        'project_id': project_id, 'export_uuid': export_operator_id,
        'snapshot_file_sha256': work_hash, 'saved_copy': str(destination), 'saved_sha256': _file_hash(destination),
        **{key: live[key] for key in ('receiver_host', 'receiver_build', 'target_scene', 'added_objects',
                                     'receiver', 'pending_imports')},
    }
    _save_ledger_atomic(stage / 'receiver.json', result)
    return result


def main():
    if sys.argv[1:] == ['--help']:
        print('Read a JSON object with binding {pid,mcp_port,work_file,scene,checkpoint} and optional baseline from stdin.')
        return 0
    if len(sys.argv) != 1:
        print(json.dumps({'ok': False, 'error': 'Receiver inspection accepts JSON on stdin only.'}))
        return 2
    try:
        request = json.load(sys.stdin.buffer)
        result = inspect_receiver(request['binding'], baseline=request.get('baseline'))
    except (KeyError, TypeError, ValueError, OSError, RuntimeError) as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}))
        return 2
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
