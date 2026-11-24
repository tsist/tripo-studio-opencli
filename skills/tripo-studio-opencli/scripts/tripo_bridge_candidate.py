"""Extract and independently reopen the static FBX scene received by Tripo Bridge."""

from array import array
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory


BLENDER_HOST = {"platform": "win32", "version": "5.3.0", "build": "02f0899d78e3"}
BLENDER_EXECUTABLE_SHA256 = "6670c69fbb562c0fd1fae1e35f5413a90781cdf7d44af151ee4c9c3b82a38174"


def _sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _run_blender(blender, source, request, action):
    command = [
        str(blender), "--background", "--factory-startup", "--disable-autoexec",
        str(source), "--python-exit-code", "11", "--python", str(Path(__file__).resolve()),
        "--", action, str(request),
    ]
    try:
        process = subprocess.run(
            command, capture_output=True, encoding="utf-8", errors="replace", timeout=180,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Blender timed out during candidate {action}.") from None
    if process.returncode != 0:
        tail = (process.stderr or process.stdout).strip()[-2200:]
        raise RuntimeError(f"Blender candidate {action} failed.\n{tail}")


def build_candidate(blender, received_blend, scene_name, object_names, expected_sha256, stage_dir):
    """Save a fresh candidate only after its native scene survives an independent cold reopen."""
    executable = Path(blender).resolve(strict=True)
    source = Path(received_blend).resolve(strict=True)
    stage = Path(stage_dir).resolve(strict=True)
    destination = stage / "candidate.blend"
    if source.suffix.lower() != ".blend":
        raise ValueError("Expected a saved receiver .blend file.")
    if not isinstance(scene_name, str) or not scene_name.strip():
        raise ValueError("Expected the saved receiver's target scene name.")
    if isinstance(object_names, (str, bytes)):
        raise ValueError("Expected an explicit list of received object names.")
    names = list(object_names)
    if not names or any(not isinstance(name, str) or not name for name in names) or len(set(names)) != len(names):
        raise ValueError("Expected unique received object names.")
    if not isinstance(expected_sha256, str) or not re.fullmatch("[0-9a-f]{64}", expected_sha256):
        raise ValueError("Expected the receiver's lowercase SHA-256 fingerprint.")
    if destination.exists():
        raise FileExistsError("The candidate destination already exists.")
    if _sha256(source) != expected_sha256:
        raise ValueError("The saved receiver file differs from its recorded SHA-256.")

    with TemporaryDirectory(prefix=".tripo-verify-", dir=stage) as folder:
        working = Path(folder)
        candidate = working / "candidate.blend"
        request = working / "request.json"
        request.write_text(json.dumps({"scene": scene_name, "objects": sorted(names)}), encoding="utf-8")
        try:
            _run_blender(executable, source, request, "extract")
            created_sha = _sha256(candidate)
            _run_blender(executable, candidate, request, "reopen")
        finally:
            if _sha256(source) != expected_sha256:
                raise RuntimeError("The receiver .blend changed during candidate verification.")
        if _sha256(candidate) != created_sha:
            raise RuntimeError("The candidate bytes changed during cold reopen.")

        selected = json.loads((working / "source-scene.json").read_bytes())
        reopened = json.loads((working / "candidate-scene.json").read_bytes())
        if selected != reopened:
            raise RuntimeError("The extracted candidate differs from the saved receiver scene.")
        # Publish the verified bytes once. The hard link fails if another
        # producer claimed the same name and does not expose a partial copy.
        os.link(candidate, destination)
    return {
        "ok": True, "candidate": str(destination), "sha256": created_sha,
        "source_sha256": expected_sha256, "scene": scene_name, "objects": sorted(names),
        "verification": {
            "cold_reopened": True, "source_unchanged": True, "scene_and_objects_match": True,
            "objects": reopened["objects"], "materials": reopened["materials"],
            "base_color_maps": reopened["base_color_maps"], "blender_build": reopened["blender_build"],
            "blender_host": reopened["blender_host"],
        },
    }


def _digest(elements, attribute, width, typecode):
    values = array(typecode, [0]) * (len(elements) * width)
    elements.foreach_get(attribute, values)
    if typecode == "f" and not all(math.isfinite(value) for value in values):
        raise ValueError(f"Non-finite data in {attribute}.")
    return hashlib.sha256(values).hexdigest()


def _mesh_state(mesh):
    if not mesh.vertices or not mesh.polygons or not mesh.uv_layers.active:
        raise ValueError(f"Mesh {mesh.name!r} has no faces or active UV map.")
    uv = {}
    for layer in mesh.uv_layers:
        if len(layer.uv) != len(mesh.loops):
            raise ValueError(f"Mesh {mesh.name!r} has an incomplete UV map.")
        uv[layer.name] = {"sha256": _digest(layer.uv, "vector", 2, "f"), "render": layer.active_render}
    return {
        "name": mesh.name, "vertices": len(mesh.vertices), "faces": len(mesh.polygons),
        "corners": len(mesh.loops), "coords_sha256": _digest(mesh.vertices, "co", 3, "f"),
        "edges_sha256": _digest(mesh.edges, "vertices", 2, "i"),
        "loop_vertices_sha256": _digest(mesh.loops, "vertex_index", 1, "i"),
        "face_offsets_sha256": _digest(mesh.polygons, "loop_start", 1, "i"),
        "face_lengths_sha256": _digest(mesh.polygons, "loop_total", 1, "i"),
        "face_materials_sha256": _digest(mesh.polygons, "material_index", 1, "i"),
        "face_smooth_sha256": _digest(mesh.polygons, "use_smooth", 1, "b"),
        "corner_normals_sha256": _digest(mesh.corner_normals, "vector", 3, "f"),
        "active_uv": mesh.uv_layers.active.name, "uv_layers": uv,
    }


def _image_state(image):
    packed = [
        {"bytes": item.packed_file.size, "sha256": hashlib.sha256(item.packed_file.data).hexdigest()}
        for item in image.packed_files
    ]
    if image.source in {"FILE", "TILED", "SEQUENCE", "MOVIE"} and not packed:
        raise ValueError(f"Image {image.name!r} is missing its embedded data.")
    return {"name": image.name, "source": image.source, "size": list(image.size),
            "colorspace": image.colorspace_settings.name, "packed": packed}


def _material_state(material):
    tree = material.node_tree
    if tree is None:
        return {"nodes": {}, "links": [], "images": {}}, []
    images = {
        node.name: _image_state(node.image)
        for node in tree.nodes if getattr(node, "image", None) is not None
    }
    # The static FBX recipe's imported materials connect their texture to a
    # Principled shader. Require that shader to feed an active Surface output.
    active_shaders = {
        link.from_node.name for link in tree.links
        if link.is_valid and not link.is_muted and link.to_node.type == "OUTPUT_MATERIAL"
        and link.to_node.is_active_output and link.to_socket.identifier == "Surface"
        and link.from_node.type == "BSDF_PRINCIPLED" and not link.from_node.mute
    }
    base_colors = [
        {"material": material.name, **images[link.from_node.name]}
        for link in tree.links
        if link.is_valid and not link.is_muted and link.to_node.name in active_shaders
        and link.to_socket.identifier == "Base Color" and link.from_socket.identifier == "Color"
        and link.from_node.type == "TEX_IMAGE" and not link.from_node.mute
        and link.from_node.name in images and images[link.from_node.name]["size"] == [8192, 8192]
        and images[link.from_node.name]["packed"]
    ]
    state = {
        "nodes": {node.name: node.bl_idname for node in tree.nodes},
        "links": sorted([link.from_node.name, link.from_socket.identifier,
                         link.to_node.name, link.to_socket.identifier] for link in tree.links),
        "images": images,
    }
    return state, base_colors


def _scene_state(bpy, scene, names):
    if sorted(scene.objects.keys()) != names:
        raise ValueError("The saved receiver scene does not contain exactly the reported objects.")
    bpy.context.window.scene = scene
    bpy.context.view_layer.update()
    objects, material_ids, used_materials = {}, {}, set()
    for name in names:
        obj = scene.objects[name]
        if obj.type not in {"EMPTY", "MESH"} or obj.animation_data is not None:
            raise ValueError("The static FBX candidate contains an animated or unsupported object.")
        record = {"type": obj.type, "parent": obj.parent.name if obj.parent else None,
                  "matrix_basis": [list(row) for row in obj.matrix_basis],
                  "matrix_parent_inverse": [list(row) for row in obj.matrix_parent_inverse],
                  "world_transform": [list(row) for row in obj.matrix_world]}
        if obj.type == "MESH":
            record["mesh"] = _mesh_state(obj.data)
            record["material_slots"] = [slot.material.name if slot.material else None for slot in obj.material_slots]
            record["mesh_materials"] = [material.name if material else None for material in obj.data.materials]
            for material in [*obj.data.materials, *(slot.material for slot in obj.material_slots)]:
                if material:
                    material_ids[material.name] = material
            used_indices = {polygon.material_index for polygon in obj.data.polygons}
            used_materials.update(record["material_slots"][i] for i in used_indices if i < len(obj.material_slots))
        objects[name] = record
    materials, base_colors = {}, []
    for name, material in material_ids.items():
        materials[name], matches = _material_state(material)
        if name in used_materials:
            base_colors.extend(matches)
    if not base_colors:
        raise ValueError("No mesh material uses an embedded 8192 × 8192 base-color map.")
    return {
        "blender_build": bpy.app.build_hash.decode("ascii"),
        "blender_host": {"platform": sys.platform, "version": ".".join(map(str, bpy.app.version)),
                         "build": bpy.app.build_hash.decode("ascii")},
        "objects": objects, "materials": materials,
        "base_color_maps": sorted(base_colors, key=lambda item: (item["material"], item["name"])),
    }


def _worker():
    import bpy

    mode, request_path = sys.argv[sys.argv.index("--") + 1:]
    request_path = Path(request_path)
    request = json.loads(request_path.read_bytes())
    if (sys.platform != BLENDER_HOST["platform"] or ".".join(map(str, bpy.app.version)) != BLENDER_HOST["version"]
            or bpy.app.build_hash.decode("ascii") != BLENDER_HOST["build"]):
        raise RuntimeError(f"Candidate verification requires Windows Blender {BLENDER_HOST['version']} build {BLENDER_HOST['build']}.")
    scene = bpy.data.scenes.get(request["scene"])
    if scene is None:
        raise ValueError("The specified receiver scene is missing from the saved .blend.")
    if mode == "extract":
        state = _scene_state(bpy, scene, request["objects"])
        library = request_path.with_name("target-scene.blend")
        bpy.data.libraries.write(str(library), {scene}, compress=True)
        bpy.ops.wm.read_factory_settings(use_empty=True)
        default_scene = bpy.context.scene
        with bpy.data.libraries.load(str(library), link=False) as (_, output):
            output.scenes = [request["scene"]]
        imported_scene = output.scenes[0]
        bpy.context.window.scene = imported_scene
        bpy.data.scenes.remove(default_scene)
        bpy.ops.wm.save_as_mainfile(filepath=str(request_path.with_name("candidate.blend")), compress=True)
        report = request_path.with_name("source-scene.json")
    elif mode == "reopen":
        if (bpy.context.scene != scene or list(bpy.data.scenes.keys()) != [scene.name]
                or sorted(bpy.data.objects.keys()) != request["objects"]
                or bpy.data.libraries):
            raise ValueError("The isolated candidate did not reopen to the target scene and objects without linked libraries.")
        state = _scene_state(bpy, scene, request["objects"])
        report = request_path.with_name("candidate-scene.json")
    else:
        raise ValueError("Unknown Blender candidate operation.")
    report.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    _worker()
