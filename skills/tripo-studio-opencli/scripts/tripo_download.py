#!/usr/bin/env python3
"""Safe download and artifact-validation helpers for the Tripo driver.

This module deliberately does not implement an HTTP downloader.  It delegates
transfers to the already-installed ``smart-download`` PowerShell skill and only
adds:

* confined destination construction;
* strict local artifact validation;
* a small bridge that keeps signed URLs out of process command lines.

The SHA-256 values produced here are local file fingerprints.  They are not
publisher signatures and must not be described as publisher-verified.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import zipfile
from pathlib import Path, PureWindowsPath
from typing import Any, Callable, Sequence


ENGINE_SCRIPT = Path(os.environ.get("TRIPO_SMART_DOWNLOAD_ENGINE", "__smart_download_not_configured__"))
WRAPPER_SCRIPT = Path(__file__).resolve().with_name("secure_download_bridge.ps1")
DEFAULT_STATE_ROOT = Path.cwd() / ".codex-downloads"

_HTTP_URL_RE = re.compile(r"https?://[^\s<>'\"]+", re.IGNORECASE)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_SHA256_RE = re.compile(r"^[A-Fa-f0-9]{64}$")
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_HTML_PREFIXES = (
    b"<!doctype html",
    b"<html",
    b"<head",
    b"<body",
    b"<script",
    b"<title",
)
_MAX_GLTF_JSON = 64 * 1024 * 1024
_MAX_ZIP_UNCOMPRESSED = 8 * 1024 * 1024 * 1024
_MAX_ZIP_ENTRIES = 10000
_GLB_JSON_CHUNK = 0x4E4F534A
_GLB_BIN_CHUNK = 0x004E4942
_FBX_BINARY_MAGIC = b"Kaydara FBX Binary  \x00"


class UnsafeDestinationError(ValueError):
    """Raised when a caller-supplied destination could escape its base."""


class SmartDownloadError(RuntimeError):
    """Raised for a bridge/engine failure.  Messages are URL-redacted."""


def _redact_urls(text: str) -> str:
    """Reduce every absolute HTTP(S) URL to scheme://host/[redacted-url]."""

    def replace(match: re.Match[str]) -> str:
        raw = match.group(0)
        try:
            scheme, remainder = raw.split("://", 1)
            host = re.split(r"[/?#]", remainder, maxsplit=1)[0]
            host = host.rsplit("@", 1)[-1]
            return f"{scheme}://{host}/[redacted-url]"
        except Exception:
            return "[redacted-url]"

    return _HTTP_URL_RE.sub(replace, text)


def _sanitize_text(text: object, *, secrets: Sequence[str] = ()) -> str:
    value = str(text)
    for secret in secrets:
        if secret:
            value = value.replace(secret, "[redacted-url]")
    return _redact_urls(value)


def _is_absolute_name(name: str) -> bool:
    pure = PureWindowsPath(name)
    if pure.drive or pure.root or pure.is_absolute():
        return True
    if name.startswith(("/", "\\")):
        return True
    return Path(name).is_absolute()


def _validate_path_component(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise UnsafeDestinationError(f"{label} must be a string.")
    if not value:
        raise UnsafeDestinationError(f"{label} is required.")
    if value != value.strip():
        raise UnsafeDestinationError(f"{label} must not have leading/trailing whitespace.")
    if _CONTROL_RE.search(value):
        raise UnsafeDestinationError(f"{label} contains control characters.")
    if _is_absolute_name(value):
        raise UnsafeDestinationError(f"{label} must not be an absolute path.")
    if value in {".", ".."} or ".." in value:
        raise UnsafeDestinationError(f"{label} must not contain '..'.")
    if "/" in value or "\\" in value:
        raise UnsafeDestinationError(f"{label} must not contain path separators.")
    if ":" in value:
        raise UnsafeDestinationError(f"{label} must not contain a Windows ADS/drive colon.")
    if re.search(r'[<>"|?*]', value):
        raise UnsafeDestinationError(f"{label} contains a character Windows forbids in filenames.")
    if value.endswith((".", " ")):
        raise UnsafeDestinationError(f"{label} must not end with a dot or space.")
    if value.split(".", 1)[0].upper() in _WINDOWS_RESERVED:
        raise UnsafeDestinationError(f"{label} is a reserved Windows device name.")
    return value


def safe_destination(base: Path, task_id: str, filename: str) -> Path:
    """Return an absolute, confined final destination; never overwrite a file.

    Both ``task_id`` and ``filename`` must be single path components.  Windows
    device names, alternate data streams, ``..`` traversal, absolute paths and
    symlink/junction escapes are rejected.  Existing final files raise
    :class:`FileExistsError`; the caller must choose a new name or inspect the
    existing artifact rather than retrying an export.
    """

    task_component = _validate_path_component(task_id, "task_id")
    file_component = _validate_path_component(filename, "filename")

    try:
        base_path = Path(base).expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise UnsafeDestinationError(f"Invalid base directory: {exc}") from exc
    if base_path.exists() and not base_path.is_dir():
        raise UnsafeDestinationError("base must be a directory.")

    candidate = base_path / task_component / file_component
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise UnsafeDestinationError(f"Destination could not be resolved safely: {exc}") from exc
    if resolved == base_path or not resolved.is_relative_to(base_path):
        raise UnsafeDestinationError("Destination escapes the configured base directory.")

    if candidate.exists() or candidate.is_symlink() or resolved.exists() or resolved.is_symlink():
        raise FileExistsError(f"Final destination already exists: {candidate}")
    return candidate


# ---------------------------------------------------------------------------
# Artifact validation
# ---------------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _looks_like_html(head: bytes) -> bool:
    probe = head.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    return probe.startswith(_HTML_PREFIXES) or b"<html" in probe[:512]


def _result(
    path: Path,
    model_format: str,
    packaging: str,
    *,
    ok: bool,
    kind: str = "",
    size: int = 0,
    digest: str = "",
    errors: Sequence[str] = (),
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    error_list = list(errors)
    return {
        "ok": ok,
        "path": str(path),
        "format": model_format,
        "packaging": packaging,
        "kind": kind,
        "bytes": size,
        "sha256": digest,
        "sha256_scope": "local_fingerprint_only",
        "publisher_verified": False,
        "validation_scope": "file_container_and_format_checks",
        "asset_usability_verified": False,
        "errors": error_list,
        "error": error_list[0] if error_list else None,
        "details": details or {},
    }


def _read_head(path: Path, limit: int = 4096) -> bytes:
    with path.open("rb") as handle:
        return handle.read(limit)


def _validate_glb(path: Path, size: int) -> tuple[bool, str, list[str], dict[str, Any]]:
    errors: list[str] = []
    details: dict[str, Any] = {}
    with path.open("rb") as handle:
        header = handle.read(12)
        if len(header) != 12:
            return False, "", ["Truncated GLB header (need 12 bytes)."], details
        magic, version, declared_length = struct.unpack("<4sII", header)
        details.update(
            magic=magic.decode("ascii", "replace"),
            glb_version=version,
            declared_length=declared_length,
        )
        if magic != b"glTF":
            return False, "", ["GLB magic is not b'glTF'."], details
        if version != 2:
            return False, "", [f"Unsupported GLB version {version}; expected 2."], details
        if declared_length != size:
            if declared_length > size:
                errors.append(
                    f"Truncated GLB: header declares {declared_length} bytes, file has {size}."
                )
            else:
                errors.append(
                    f"Trailing data after GLB: header declares {declared_length}, file has {size}."
                )

        offset = 12
        chunks: list[dict[str, Any]] = []
        json_object: dict[str, Any] | None = None
        saw_bin = False
        while offset < min(declared_length, size):
            if offset + 8 > size:
                errors.append("Truncated GLB chunk header.")
                break
            handle.seek(offset)
            chunk_header = handle.read(8)
            if len(chunk_header) != 8:
                errors.append("Truncated GLB chunk header.")
                break
            chunk_length, chunk_type = struct.unpack("<II", chunk_header)
            chunk_name = (
                "JSON" if chunk_type == _GLB_JSON_CHUNK
                else "BIN" if chunk_type == _GLB_BIN_CHUNK
                else f"0x{chunk_type:08X}"
            )
            chunks.append({"type": chunk_name, "length": chunk_length})
            if chunk_length % 4:
                errors.append(f"GLB chunk {chunk_name} length is not 4-byte aligned.")
            data_start = offset + 8
            data_end = data_start + chunk_length
            if data_end > size:
                errors.append(f"Truncated GLB {chunk_name} chunk.")
                break
            if not chunks[:-1] and chunk_type != _GLB_JSON_CHUNK:
                errors.append("First GLB chunk is not JSON.")
            if chunk_type == _GLB_JSON_CHUNK:
                if json_object is not None:
                    errors.append("GLB contains more than one JSON chunk.")
                elif chunk_length == 0:
                    errors.append("GLB JSON chunk is empty.")
                elif chunk_length > _MAX_GLTF_JSON:
                    errors.append("GLB JSON chunk is unreasonably large.")
                else:
                    handle.seek(data_start)
                    raw_json = handle.read(chunk_length)
                    try:
                        parsed = json.loads(raw_json.decode("utf-8"))
                        if isinstance(parsed, dict):
                            json_object = parsed
                        else:
                            errors.append("GLB JSON chunk is not a JSON object.")
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        errors.append(f"GLB JSON chunk is invalid: {exc}")
            elif chunk_type == _GLB_BIN_CHUNK:
                if saw_bin:
                    errors.append("GLB contains more than one BIN chunk.")
                saw_bin = True
            offset = data_end

        if offset != declared_length:
            errors.append("GLB chunk table does not end at the declared length.")
        if json_object is None:
            errors.append("GLB has no parseable JSON chunk.")
        else:
            asset = json_object.get("asset")
            asset_version = asset.get("version") if isinstance(asset, dict) else None
            details["asset_version"] = asset_version
            if not isinstance(asset_version, str) or not asset_version.startswith("2."):
                errors.append("GLB asset.version is missing or not glTF 2.x.")
        details["chunks"] = chunks
    return not errors, "GLB", errors, details


def _validate_gltf_json(path: Path, size: int) -> tuple[bool, str, list[str], dict[str, Any]]:
    errors: list[str] = []
    details: dict[str, Any] = {}
    if size > _MAX_GLTF_JSON:
        return False, "", ["glTF JSON exceeds the local validation size limit."], details
    try:
        raw = path.read_text(encoding="utf-8-sig")
        parsed = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return False, "", [f"Invalid glTF JSON: {exc}"], details
    if not isinstance(parsed, dict):
        return False, "", ["glTF root is not a JSON object."], details
    asset = parsed.get("asset")
    asset_version = asset.get("version") if isinstance(asset, dict) else None
    details["asset_version"] = asset_version
    if not isinstance(asset_version, str) or not asset_version.startswith("2."):
        errors.append("glTF asset.version is missing or not 2.x.")
    return not errors, "GLTF_JSON", errors, details


def _validate_fbx(path: Path, size: int) -> tuple[bool, str, list[str], dict[str, Any]]:
    details: dict[str, Any] = {}
    with path.open("rb") as handle:
        head = handle.read(max(65536, 27))
    if head.startswith(_FBX_BINARY_MAGIC):
        if len(head) < 27:
            return False, "", ["Truncated FBX binary header."], details
        if len(head) >= 27 and head[21:23] == b"\x1a\x00":
            version = struct.unpack("<I", head[23:27])[0]
            layout = "standard"
        elif len(head) >= 25:
            # Some hand-built fixtures omit the standard 0x1A 0x00 prelude.
            # Accept only a plausible version in that compact layout.
            version = struct.unpack("<I", head[21:25])[0]
            layout = "compact_fixture"
        else:
            return False, "", ["Truncated FBX binary version field."], details
        details.update(fbx_version=version, header_layout=layout)
        if not 6000 <= version <= 8000:
            return False, "", [f"Invalid FBX binary version {version}."], details
        return True, "FBX_BINARY", [], details

    if b"\x00" in head:
        return False, "", ["FBX file has no valid binary magic and contains NUL bytes."], details
    try:
        text = head.decode("utf-8-sig", "replace")
    except Exception:
        return False, "", ["FBX ASCII header is not decodable."], details
    header_match = re.search(r"(?m)^\s*;\s*FBX\s+(\d+\.\d+)", text)
    if not header_match:
        return False, "", ["Missing ASCII FBX header ('; FBX x.y')."], details
    if "FBXHeaderExtension" not in text and "FBXVersion" not in text:
        return False, "", ["ASCII FBX header lacks FBXHeaderExtension/FBXVersion."], details
    details["fbx_version_text"] = header_match.group(1)
    return True, "FBX_ASCII", [], details


def _validate_obj(path: Path) -> tuple[bool, str, list[str], dict[str, Any]]:
    details: dict[str, Any] = {}
    saw_vertex = False
    saw_face = False
    scanned = 0
    try:
        with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
            for line in handle:
                scanned += len(line)
                if "\x00" in line:
                    return False, "", ["OBJ contains NUL bytes."], details
                stripped = line.strip()
                if re.match(
                    r"^v\s+[-+0-9.eE]+\s+[-+0-9.eE]+\s+[-+0-9.eE]+", stripped
                ):
                    saw_vertex = True
                elif re.match(r"^f\s+\S+", stripped):
                    saw_face = True
                if saw_vertex and saw_face:
                    break
                if scanned > 64 * 1024 * 1024:
                    break
    except OSError as exc:
        return False, "", [f"OBJ could not be read: {exc}"], details
    details.update(saw_vertex=saw_vertex, saw_face=saw_face)
    errors: list[str] = []
    if not saw_vertex:
        errors.append("OBJ has no valid vertex line.")
    if not saw_face:
        errors.append("OBJ has no valid face line.")
    return not errors, "OBJ", errors, details


def _validate_stl(path: Path, size: int) -> tuple[bool, str, list[str], dict[str, Any]]:
    details: dict[str, Any] = {}
    if size >= 84:
        with path.open("rb") as handle:
            header = handle.read(84)
        triangle_count = struct.unpack("<I", header[80:84])[0]
        expected = 84 + 50 * triangle_count
        if expected == size:
            details.update(layout="binary", triangles=triangle_count)
            if triangle_count == 0:
                return False, "", ["Binary STL contains zero triangles."], details
            return True, "STL_BINARY", [], details

    with path.open("rb") as handle:
        head = handle.read(65536)
        if size > 65536:
            handle.seek(max(0, size - 65536))
            tail = handle.read(65536)
        else:
            tail = head
    probe = head.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    errors: list[str] = []
    if not probe.startswith(b"solid"):
        errors.append("STL is neither a size-consistent binary STL nor an ASCII 'solid' file.")
    if b"facet" not in head.lower() and b"facet" not in tail.lower():
        errors.append("ASCII STL has no facet records.")
    if b"vertex" not in head.lower() and b"vertex" not in tail.lower():
        errors.append("ASCII STL has no vertex records.")
    if b"endsolid" not in tail.lower():
        errors.append("ASCII STL is missing its endsolid terminator (truncated).")
    details.update(layout="ascii")
    return not errors, "STL_ASCII", errors, details


def _unsafe_zip_name(name: str) -> bool:
    if not name or "\x00" in name:
        return True
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        return True
    if ":" in normalized:
        return True
    parts = normalized.split("/")
    return any(part in {"", ".", ".."} for part in parts[:-1]) or parts[-1] in {".", ".."}


def _zip_model_suffixes(model_format: str) -> tuple[str, ...]:
    return {
        "gltf": (".glb", ".gltf"),
        "fbx": (".fbx",),
        "obj": (".obj",),
        "stl": (".stl",),
        "usdz": (".usdc", ".usda", ".usd"),
        "3mf": (".model",),
    }[model_format]


def _zip_member_header(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    model_format: str,
) -> list[str]:
    errors: list[str] = []
    with archive.open(info) as handle:
        head = handle.read(1024)
    if _looks_like_html(head):
        return [f"ZIP model member {info.filename!r} contains an HTML page."]
    lower_name = info.filename.lower()
    if model_format == "gltf" and lower_name.endswith(".glb"):
        if info.file_size < 12:
            errors.append(f"ZIP GLB member {info.filename!r} is truncated.")
        else:
            magic, version, declared = struct.unpack("<4sII", head[:12])
            if magic != b"glTF" or version != 2:
                errors.append(f"ZIP GLB member {info.filename!r} has an invalid header.")
            elif declared != info.file_size:
                errors.append(
                    f"ZIP GLB member {info.filename!r} length mismatch "
                    f"({declared} declared, {info.file_size} stored)."
                )
    elif model_format == "gltf" and lower_name.endswith(".gltf"):
        if info.file_size > _MAX_GLTF_JSON:
            errors.append(f"ZIP glTF member {info.filename!r} is unreasonably large.")
        else:
            try:
                payload = archive.read(info)
                parsed = json.loads(payload.decode("utf-8-sig"))
                asset = parsed.get("asset") if isinstance(parsed, dict) else None
                version = asset.get("version") if isinstance(asset, dict) else None
                if not isinstance(version, str) or not version.startswith("2."):
                    errors.append(f"ZIP glTF member {info.filename!r} lacks asset.version 2.x.")
            except (UnicodeDecodeError, json.JSONDecodeError, RuntimeError) as exc:
                errors.append(f"ZIP glTF member {info.filename!r} is invalid: {exc}")
    elif model_format == "fbx" and lower_name.endswith(".fbx"):
        if head.startswith(_FBX_BINARY_MAGIC):
            if len(head) < 27:
                errors.append(f"ZIP FBX member {info.filename!r} has a truncated binary header.")
        elif head.startswith(b"; FBX") and b"FBXHeaderExtension" in head:
            pass
        else:
            errors.append(f"ZIP FBX member {info.filename!r} has an invalid FBX header.")
    elif model_format == "stl" and lower_name.endswith(".stl"):
        if info.file_size >= 84 and head[80:84]:
            count = struct.unpack("<I", head[80:84])[0]
            if 84 + 50 * count != info.file_size and not head.lstrip().lower().startswith(b"solid"):
                errors.append(f"ZIP STL member {info.filename!r} is truncated or malformed.")
    return errors


def _validate_zip(
    path: Path,
    model_format: str,
) -> tuple[bool, str, list[str], dict[str, Any]]:
    errors: list[str] = []
    details: dict[str, Any] = {}
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            details["entries"] = len(infos)
            if not infos:
                return False, "", ["ZIP archive is empty."], details
            if len(infos) > _MAX_ZIP_ENTRIES:
                return False, "", ["ZIP archive has too many entries."], details

            names_seen: set[str] = set()
            total_uncompressed = 0
            for info in infos:
                total_uncompressed += info.file_size
                if total_uncompressed > _MAX_ZIP_UNCOMPRESSED:
                    return False, "", ["ZIP uncompressed size limit exceeded."], details
                if info.flag_bits & 0x1:
                    return False, "", ["Encrypted ZIP entries are not supported."], details
                if _unsafe_zip_name(info.filename):
                    return False, "", [
                        f"Unsafe ZIP member name: {info.filename!r}"
                    ], details
                key = info.filename.replace("\\", "/").casefold()
                if not info.is_dir() and key in names_seen:
                    return False, "", [f"Duplicate ZIP member name: {info.filename!r}"], details
                names_seen.add(key)

            bad_member = archive.testzip()
            if bad_member is not None:
                return False, "", [f"ZIP CRC failure in member {bad_member!r}."], details

            files = [info for info in infos if not info.is_dir()]
            lower_names = [info.filename.casefold() for info in files]
            if model_format == "3mf":
                has_content_types = any(
                    name == "[content_types].xml" for name in lower_names
                )
                model_infos = [
                    info for info in files
                    if re.match(r"^3d/[^/]+\.model$", info.filename, re.IGNORECASE)
                ]
                if not has_content_types:
                    errors.append("3MF ZIP lacks [Content_Types].xml.")
                if not model_infos:
                    errors.append("3MF ZIP lacks a 3D/*.model member.")
                kind = "3MF"
            elif model_format == "usdz":
                model_infos = [
                    info for info in files
                    if info.filename.casefold().endswith((".usdc", ".usda", ".usd"))
                ]
                if not model_infos:
                    errors.append("USDZ ZIP lacks a .usdc/.usda/.usd member.")
                kind = "USDZ"
            else:
                suffixes = _zip_model_suffixes(model_format)
                model_infos = [
                    info for info in files
                    if info.filename.casefold().endswith(suffixes)
                ]
                if not model_infos:
                    errors.append(
                        f"ZIP archive has no model member with suffix {suffixes}."
                    )
                kind = "ZIP"

            details["model_entries"] = [info.filename for info in model_infos]
            for info in model_infos[:4]:
                errors.extend(_zip_member_header(archive, info, model_format))
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, RuntimeError) as exc:
        return False, "", [f"ZIP integrity check failed: {exc}"], details
    return not errors, kind, errors, details


def validate_artifact(path: Path | str, format: str, packaging: str) -> dict[str, Any]:
    """Strictly validate a downloaded artifact without executing it.

    Expected results include ``ok``, ``errors``, ``kind`` and a local
    ``sha256`` fingerprint.  ``publisher_verified`` is always false: the hash is
    computed from the downloaded bytes and is not a publisher signature.
    """

    model_format = str(format or "").strip().lower()
    if model_format == "glb":
        model_format = "gltf"
    pack = str(packaging or "").strip().lower()
    target = Path(path)

    if model_format not in {"gltf", "fbx", "obj", "stl", "usdz", "3mf"}:
        return _result(
            target, model_format, pack, ok=False,
            errors=[f"Unsupported model format: {model_format!r}"],
        )
    if pack not in {"embedded", "zip"}:
        return _result(
            target, model_format, pack, ok=False,
            errors=[f"Unsupported texture packaging: {pack!r}"],
        )

    try:
        stat = target.stat()
    except OSError as exc:
        return _result(
            target, model_format, pack, ok=False,
            errors=[f"Artifact is missing or unreadable: {exc}"],
        )
    if not target.is_file():
        return _result(
            target, model_format, pack, ok=False,
            errors=["Artifact path is not a regular file."],
        )
    size = int(stat.st_size)
    if size <= 0:
        return _result(
            target, model_format, pack, ok=False,
            errors=["Artifact is empty."],
        )

    try:
        digest = _sha256_file(target)
    except OSError as exc:
        return _result(
            target, model_format, pack, ok=False, size=size,
            errors=[f"SHA-256 could not be computed: {exc}"],
        )

    head = _read_head(target)
    if _looks_like_html(head):
        return _result(
            target, model_format, pack, ok=False, size=size, digest=digest,
            errors=["Artifact is an HTML page, not a model file."],
        )

    try:
        if model_format in {"usdz", "3mf"} or (pack == "zip" and model_format != "gltf"):
            ok, kind, errors, details = _validate_zip(target, model_format)
        elif pack == "zip":
            ok, kind, errors, details = _validate_zip(target, model_format)
        elif head.startswith(b"PK"):
            return _result(
                target, model_format, pack, ok=False, size=size, digest=digest,
                errors=["Artifact is a ZIP archive but packaging is 'embedded'."],
            )
        elif model_format == "gltf":
            if head.startswith(b"glTF"):
                ok, kind, errors, details = _validate_glb(target, size)
            elif head.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"{"):
                ok, kind, errors, details = _validate_gltf_json(target, size)
            else:
                ok, kind, errors, details = False, "", [
                    "glTF artifact is neither a GLB nor a glTF JSON document."
                ], {}
        elif model_format == "fbx":
            ok, kind, errors, details = _validate_fbx(target, size)
        elif model_format == "obj":
            ok, kind, errors, details = _validate_obj(target)
        elif model_format == "stl":
            ok, kind, errors, details = _validate_stl(target, size)
        else:
            ok, kind, errors, details = False, "", [
                f"No validator implemented for format {model_format!r}."
            ], {}
    except Exception as exc:
        return _result(
            target, model_format, pack, ok=False, size=size, digest=digest,
            errors=[f"Validation failed unexpectedly: {exc}"],
        )

    return _result(
        target, model_format, pack,
        ok=ok, kind=kind, size=size, digest=digest,
        errors=errors, details=details,
    )


# ---------------------------------------------------------------------------
# smart-download bridge
# ---------------------------------------------------------------------------


def _validate_url(url: object) -> str:
    if not isinstance(url, str) or not url:
        raise SmartDownloadError("URL must be a non-empty string.")
    if _CONTROL_RE.search(url) or any(char.isspace() for char in url):
        raise SmartDownloadError("URL must not contain whitespace or control characters.")
    try:
        scheme, separator, remainder = url.partition("://")
        if not separator or not remainder:
            raise ValueError
        if scheme.lower() not in {"http", "https"}:
            raise SmartDownloadError("Only http and https URLs are allowed.")
        netloc = re.split(r"[/?#]", remainder, maxsplit=1)[0]
        if not netloc or "@@" in netloc:
            raise ValueError
    except SmartDownloadError:
        raise
    except Exception:
        raise SmartDownloadError("URL is not a valid absolute http(s) URL.") from None
    return url


def _normalize_absolute_path(value: object, label: str) -> Path:
    try:
        raw = os.fspath(value)  # type: ignore[arg-type]
    except TypeError:
        raise SmartDownloadError(f"{label} must be a filesystem path.") from None
    if not isinstance(raw, str):
        raise SmartDownloadError(f"{label} must be a string path.")
    if not raw or _CONTROL_RE.search(raw):
        raise SmartDownloadError(f"{label} is empty or contains control characters.")
    if raw.startswith(("\\\\?\\", "\\\\.\\")):
        raise SmartDownloadError(f"{label} must not use a Windows device path.")
    pure = PureWindowsPath(raw)
    if not pure.is_absolute() or not pure.drive:
        raise SmartDownloadError(f"{label} must be an absolute path.")
    if any(part in {".", ".."} for part in pure.parts[1:]):
        raise SmartDownloadError(f"{label} must not contain '.' or '..' components.")
    tail = raw[2:] if len(raw) >= 2 and raw[1] == ":" else raw
    if ":" in tail:
        raise SmartDownloadError(f"{label} must not contain a Windows ADS colon.")
    path = Path(raw)
    if not path.name:
        raise SmartDownloadError(f"{label} must include a file/directory name.")
    return path


def _validate_destination_path(destination: object) -> Path:
    path = _normalize_absolute_path(destination, "destination")
    _validate_path_component(path.name, "destination filename")
    if path.exists() or path.is_symlink():
        raise FileExistsError(f"Final destination already exists: {path}")
    return path


def _validate_state_root(state_root: object) -> Path:
    path = _normalize_absolute_path(state_root, "state_root")
    _validate_path_component(path.name, "state_root name")
    return path


def _extract_json_object(text: str) -> dict[str, Any]:
    start = text.find("{")
    if start < 0:
        raise ValueError("No JSON object in bridge output.")
    value, _ = json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(value, dict):
        raise ValueError("Bridge output JSON is not an object.")
    return value


class SmartDownloadBridge:
    """Thin Python facade over the installed smart-download PowerShell skill.

    ``start`` sends the signed URL only through a UTF-8 JSON stdin payload.  The
    PowerShell wrapper reads that payload into memory and passes ``-Uri`` to the
    original engine as a script parameter; the URL never enters an OS command
    line.  The engine starts its own hidden background worker and returns a
    compact job object without waiting for bytes.

    Returned/raised text is URL-redacted.  ``status`` only queries an existing
    job; this class never starts a retry.
    """

    def __init__(
        self,
        state_root: Path | str | None = None,
        *,
        wrapper: Path | str = WRAPPER_SCRIPT,
        powershell: str | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        timeout: float = 60.0,
    ) -> None:
        self.state_root = _validate_state_root(
            state_root if state_root is not None else DEFAULT_STATE_ROOT
        )
        self.wrapper = Path(wrapper)
        self.powershell = powershell or shutil.which("powershell.exe") or "powershell.exe"
        self._runner = runner or subprocess.run
        self.timeout = float(timeout)

    def _call(
        self,
        arguments: Sequence[str],
        *,
        stdin_payload: str | None = None,
        timeout: float | None = None,
        secret: str | None = None,
    ) -> dict[str, Any]:
        if not self.wrapper.is_file():
            raise SmartDownloadError(f"Bridge wrapper is missing: {self.wrapper}")
        if not ENGINE_SCRIPT.is_file():
            raise SmartDownloadError(f"smart-download engine is missing: {ENGINE_SCRIPT}")

        command = [
            self.powershell,
            "-NoProfile",
            "-ExecutionPolicy", "Bypass",
            "-File", str(self.wrapper),
            *arguments,
        ]
        kwargs: dict[str, Any] = {
            "input": stdin_payload,
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "timeout": timeout if timeout is not None else self.timeout,
        }
        if stdin_payload is None:
            kwargs["stdin"] = subprocess.DEVNULL
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        try:
            completed = self._runner(command, **kwargs)
        except subprocess.TimeoutExpired:
            raise SmartDownloadError(
                f"Download bridge timed out after {kwargs['timeout']} seconds."
            ) from None
        except OSError as exc:
            raise SmartDownloadError(
                _sanitize_text(f"Download bridge could not start: {exc}", secrets=(secret or "",))
            ) from None

        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        try:
            data = _extract_json_object(stdout)
        except (ValueError, json.JSONDecodeError):
            detail = stderr.strip() or stdout.strip() or "no output"
            raise SmartDownloadError(
                _sanitize_text(
                    f"Bridge returned no JSON (exit {completed.returncode}): {detail[-2000:]}",
                    secrets=(secret or "",),
                )
            ) from None

        if data.get("ok") is False:
            raise SmartDownloadError(
                _sanitize_text(data.get("error") or "Bridge reported failure.", secrets=(secret or "",))
            )
        if completed.returncode != 0:
            detail = data.get("error") or stderr or stdout
            raise SmartDownloadError(
                _sanitize_text(
                    f"Bridge exited {completed.returncode}: {detail}",
                    secrets=(secret or "",),
                )
            )
        return self._sanitize_result(data, secret=secret)

    @staticmethod
    def _sanitize_result(value: Any, *, secret: str | None = None) -> Any:
        if isinstance(value, str):
            return _sanitize_text(value, secrets=(secret or "",))
        if isinstance(value, list):
            return [SmartDownloadBridge._sanitize_result(item, secret=secret) for item in value]
        if isinstance(value, dict):
            return {
                key: SmartDownloadBridge._sanitize_result(item, secret=secret)
                for key, item in value.items()
            }
        return value

    def doctor(self) -> dict[str, Any]:
        """Run the engine's no-network Doctor action through the wrapper."""

        return self._call(
            ["-Action", "Doctor", "-StateRoot", str(self.state_root)],
            timeout=min(self.timeout, 30.0),
        )

    def start(
        self,
        url: str,
        destination: Path | str,
        expected_bytes: int | None = None,
        sha256: str | None = None,
    ) -> dict[str, Any]:
        """Start one background download and return the engine's compact job.

        No retry is performed.  The signed URL is written only to the wrapper's
        stdin pipe, never to argv/log output/exceptions created by this module.
        """

        validated_url = _validate_url(url)
        dest = _validate_destination_path(destination)

        if expected_bytes is not None:
            if isinstance(expected_bytes, bool) or not isinstance(expected_bytes, int):
                raise SmartDownloadError("expected_bytes must be a positive integer or None.")
            if expected_bytes <= 0:
                raise SmartDownloadError("expected_bytes must be greater than zero when supplied.")
        if sha256 is not None:
            if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256):
                raise SmartDownloadError("sha256 must contain exactly 64 hexadecimal characters.")
            sha256 = sha256.lower()

        payload = {
            "url": validated_url,
            "destination": str(dest),
            "state_root": str(self.state_root),
            "expected_bytes": expected_bytes,
            "sha256": sha256,
            "connections": 4,
        }
        stdin_payload = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
        data = self._call(
            ["-Action", "Download"],
            stdin_payload=stdin_payload,
            secret=validated_url,
        )
        if not isinstance(data.get("id"), str) or not data["id"]:
            raise SmartDownloadError(
                _sanitize_text(
                    f"Engine did not return a job id (status={data.get('status')!r}).",
                    secrets=(validated_url,),
                )
            )
        return data

    def status(
        self,
        job_id: str,
        state_root: Path | str | None = None,
    ) -> dict[str, Any]:
        """Query one existing job; never starts or retries a download."""

        if not isinstance(job_id, str) or not _JOB_ID_RE.fullmatch(job_id):
            raise SmartDownloadError("Invalid job_id.")
        root = _validate_state_root(state_root if state_root is not None else self.state_root)
        return self._call(
            ["-Action", "Status", "-JobId", job_id, "-StateRoot", str(root)],
            timeout=min(self.timeout, 30.0),
        )


__all__ = [
    "DEFAULT_STATE_ROOT",
    "ENGINE_SCRIPT",
    "SmartDownloadBridge",
    "SmartDownloadError",
    "UnsafeDestinationError",
    "WRAPPER_SCRIPT",
    "safe_destination",
    "validate_artifact",
]
