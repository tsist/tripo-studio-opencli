"""
Headless Blender 5.2 native FBX importer for Tripo Studio outputs (Smart Mesh P2.0 quad FBX & Auto-Rigged FBX).
Preserves quad topology, skeletal hierarchy, vertex weights, embedded/packed textures, and actions,
saves a standalone .blend file, and writes a structured structural telemetry JSON report.

Usage (via blenderctl / blender --factory-startup -b):
  blender.exe --background --factory-startup --disable-autoexec --offline-mode \
    --python-exit-code 23 --python fbx_import_headless.py -- \
    <input_fbx> <output_blend> <report_json> <front_axis> <source_sha256> <project_id>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import bmesh
import bpy
from mathutils import Vector


def _clear_scene() -> None:
    bpy.ops.wm.read_factory_settings(use_empty=True)


def _import_fbx(filepath: Path) -> None:
    op = bpy.ops.import_scene.fbx
    supported = {p.identifier for p in op.get_rna_type().properties}
    kwargs: dict[str, object] = {"filepath": str(filepath)}
    preferred = {
        "use_custom_normals": True,
        # Tripo exports embed their images. Recursive fallback may otherwise
        # walk far outside the asset directory for exporter-relative paths.
        # Normal relative-path and embedded image loading remain available;
        # unresolved external dependencies are reported instead of searched.
        "use_image_search": False,
        "ignore_leaf_bones": False,
        "automatic_bone_orientation": True,
        "use_anim": True,
    }
    for key, val in preferred.items():
        if key in supported:
            kwargs[key] = val
    result = op(**kwargs)
    if "FINISHED" not in result:
        raise RuntimeError(f"import_scene.fbx did not finish cleanly: {result}")


def _pack_external_images() -> list[str]:
    unpacked = [
        img.name
        for img in bpy.data.images
        if img.source == "FILE" and img.packed_file is None and img.filepath
    ]
    if unpacked:
        try:
            bpy.ops.file.pack_all()
        except Exception:
            # Individual packs handled best-effort; missing external files are surfaced in report
            for img in bpy.data.images:
                if img.source == "FILE" and img.packed_file is None and img.filepath:
                    try:
                        img.pack()
                    except Exception:
                        pass
    return unpacked


def _collect_mesh_stats(obj: bpy.types.Object) -> dict:
    bm = bmesh.new()
    try:
        bm.from_mesh(obj.data)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()

        tris = sum(1 for f in bm.faces if len(f.verts) == 3)
        quads = sum(1 for f in bm.faces if len(f.verts) == 4)
        ngons = sum(1 for f in bm.faces if len(f.verts) > 4)
        face_count = len(bm.faces)
        non_manifold_edges = sum(1 for e in bm.edges if not e.is_manifold)
        loose_verts = sum(1 for v in bm.verts if not v.link_edges)

        return {
            "name": obj.name,
            "vertices": len(bm.verts),
            "edges": len(bm.edges),
            "faces": face_count,
            "triangles": tris,
            "quads": quads,
            "ngons": ngons,
            "quad_ratio": round(quads / face_count, 4) if face_count else 0.0,
            "non_manifold_edges": non_manifold_edges,
            "loose_vertices": loose_verts,
            "uv_layers": [uv.name for uv in obj.data.uv_layers],
            "material_slots": [m.material.name if m.material else None for m in obj.material_slots],
            "vertex_groups": len(obj.vertex_groups),
        }
    finally:
        bm.free()


def _world_bounds(mesh_objects: list[bpy.types.Object]) -> dict | None:
    if not mesh_objects:
        return None
    mins = Vector((float("inf"), float("inf"), float("inf")))
    maxs = Vector((float("-inf"), float("-inf"), float("-inf")))
    for obj in mesh_objects:
        for corner in obj.bound_box:
            wc = obj.matrix_world @ Vector(corner)
            mins.x = min(mins.x, wc.x)
            mins.y = min(mins.y, wc.y)
            mins.z = min(mins.z, wc.z)
            maxs.x = max(maxs.x, wc.x)
            maxs.y = max(maxs.y, wc.y)
            maxs.z = max(maxs.z, wc.z)
    dims = maxs - mins
    return {
        "min": [round(mins.x, 5), round(mins.y, 5), round(mins.z, 5)],
        "max": [round(maxs.x, 5), round(maxs.y, 5), round(maxs.z, 5)],
        "dimensions": [round(dims.x, 5), round(dims.y, 5), round(dims.z, 5)],
    }


def main() -> int:
    argv = sys.argv
    if "--" not in argv:
        raise RuntimeError("Missing `--` script arguments separator")
    args = argv[argv.index("--") + 1 :]
    if len(args) < 6:
        raise RuntimeError(
            "Expected 6 args: <input_fbx> <output_blend> <report_json> <front_axis> <source_sha256> <project_id>"
        )

    input_fbx = Path(args[0]).resolve()
    output_blend = Path(args[1]).resolve()
    report_json = Path(args[2]).resolve()
    front_axis = args[3]
    source_sha256 = args[4]
    project_id = args[5]

    if not input_fbx.exists():
        raise FileNotFoundError(f"Input FBX missing: {input_fbx}")

    if output_blend == input_fbx or report_json == input_fbx:
        raise ValueError(
            "Refusing to write output/report over the input FBX: "
            f"{input_fbx}"
        )
    if output_blend == report_json:
        raise ValueError(
            "Refusing to use the same path for output_blend and report_json: "
            f"{output_blend}"
        )
    existing_outputs = [
        str(path) for path in (output_blend, report_json) if path.exists()
    ]
    if existing_outputs:
        raise FileExistsError(
            "Refusing to overwrite existing output(s): "
            + ", ".join(existing_outputs)
        )

    _clear_scene()
    _import_fbx(input_fbx)
    depsgraph = bpy.context.evaluated_depsgraph_get()
    depsgraph.update()

    initially_unpacked = _pack_external_images()

    scene = bpy.context.scene
    scene["tripo_studio_source_file"] = str(input_fbx)
    scene["tripo_studio_source_sha256"] = source_sha256
    scene["tripo_studio_project_id"] = project_id
    scene["tripo_studio_front_axis"] = front_axis
    scene["tripo_studio_orientation_transform_applied"] = False
    scene["tripo_studio_orientation_semantics"] = "declared_source_front_for_preview"
    scene["tripo_studio_importer"] = "blender_5_2_native_fbx"

    mesh_objs = [o for o in scene.objects if o.type == "MESH"]
    armature_objs = [o for o in scene.objects if o.type == "ARMATURE"]
    meshes_report = [_collect_mesh_stats(o) for o in mesh_objs]

    total_faces = sum(m["faces"] for m in meshes_report)
    total_quads = sum(m["quads"] for m in meshes_report)
    images_report = [
        {
            "name": img.name,
            "size": list(img.size),
            "packed": img.packed_file is not None,
            "filepath": img.filepath,
        }
        for img in bpy.data.images
        if img.source != "VIEWER"
    ]
    armatures_report = [
        {
            "name": arm.name,
            "bones": len(arm.data.bones),
            "root_bones": [b.name for b in arm.data.bones if b.parent is None],
        }
        for arm in armature_objs
    ]
    actions_report = [
        {
            "name": act.name,
            "frame_range": [round(act.frame_range[0], 3), round(act.frame_range[1], 3)],
        }
        for act in bpy.data.actions
    ]

    output_blend.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(output_blend), compress=False)

    report = {
        "ok": True,
        "blender_version": ".".join(str(x) for x in bpy.app.version),
        "input_fbx": str(input_fbx),
        "source_sha256": source_sha256,
        "output_blend": str(output_blend),
        "project_id": project_id,
        "front_axis": front_axis,
        "orientation_transform_applied": False,
        "orientation_semantics": "declared_source_front_for_preview",
        "object_count": len(scene.objects),
        "mesh_count": len(mesh_objs),
        "armature_count": len(armature_objs),
        "total_faces": total_faces,
        "total_quads": total_quads,
        "overall_quad_ratio": round(total_quads / total_faces, 4) if total_faces else 0.0,
        "world_bounds": _world_bounds(mesh_objs),
        "initially_unpacked_images": initially_unpacked,
        "meshes": meshes_report,
        "armatures": armatures_report,
        "materials": [mat.name for mat in bpy.data.materials],
        "images": images_report,
        "actions": actions_report,
    }

    report_json.parent.mkdir(parents=True, exist_ok=True)
    report_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
