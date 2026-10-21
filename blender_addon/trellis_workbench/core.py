"""Scene operations behind the TRELLIS Workbench operators.

Everything here runs on Blender's main thread (operators or timers). It never opens a .blend
as a replacement for the current file, never edits datablocks it did not create (body meshes,
armatures, actions and materials are read-only inputs), and rolls back on failure.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from contextlib import contextmanager
from pathlib import Path

import bpy
import numpy as np
from mathutils import Matrix, Vector
from mathutils.bvhtree import BVHTree

ID_COLLECTIONS = (
    "objects",
    "meshes",
    "materials",
    "images",
    "textures",
    "node_groups",
    "collections",
    "actions",
    "armatures",
    "cameras",
    "lights",
    "curves",
)
SIDE_RE = re.compile(r"(?:[._-](?P<a>[LR])$)|(?:^(?P<b>Left|Right))|(?:(?P<c>Left|Right)$)", re.IGNORECASE)


class WorkbenchError(RuntimeError):
    pass


# --- safety helpers ----------------------------------------------------------------------------


def _ensure_editable(obj: bpy.types.Object) -> None:
    if obj.library is not None or obj.override_library is not None:
        raise WorkbenchError(f"{obj.name} is linked or a library override; refusing to modify it")


def _ids_snapshot() -> dict[str, set[int]]:
    return {name: {idb.as_pointer() for idb in getattr(bpy.data, name)} for name in ID_COLLECTIONS}


def _remove_new_ids(before: dict[str, set[int]]) -> int:
    removed = 0
    # Objects first so meshes/materials lose their users.
    for name in ID_COLLECTIONS:
        coll = getattr(bpy.data, name)
        for idb in [i for i in coll if i.as_pointer() not in before[name]]:
            coll.remove(idb, do_unlink=True)
            removed += 1
    return removed


@contextmanager
def preserved_ui_state(context):
    """Restore mode, selection, active object and active collection afterwards."""
    view_layer = context.view_layer
    active = view_layer.objects.active
    mode = context.mode
    if mode != "OBJECT" and active is not None:
        bpy.ops.object.mode_set(mode="OBJECT")
    selected = [o.name for o in context.selected_objects]
    active_lc = view_layer.active_layer_collection
    try:
        yield
    finally:
        for o in view_layer.objects:
            o.select_set(o.name in selected)
        if active is not None and active.name in view_layer.objects:
            view_layer.objects.active = active
        view_layer.active_layer_collection = active_lc
        if mode.startswith("EDIT") and active is not None and active.type == "MESH":
            bpy.ops.object.mode_set(mode="EDIT")
        elif mode == "POSE" and active is not None and active.type == "ARMATURE":
            bpy.ops.object.mode_set(mode="POSE")


def _find_layer_collection(layer_coll, coll):
    if layer_coll.collection == coll:
        return layer_coll
    for child in layer_coll.children:
        found = _find_layer_collection(child, coll)
        if found:
            return found
    return None


# --- bundles -----------------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_bundle(bundle_dir: str | Path) -> dict:
    """Parse and checksum-verify a published bundle. Only manifest-listed files are trusted."""
    d = Path(bundle_dir)
    manifest = json.loads((d / "manifest.json").read_text())
    if manifest.get("schema_version") != 1:
        raise WorkbenchError(f"unsupported bundle schema {manifest.get('schema_version')!r}")
    for rec in manifest["files"]:
        rel = Path(rec["path"])
        if rel.is_absolute() or ".." in rel.parts:
            raise WorkbenchError(f"unsafe path in manifest: {rec['path']}")
        p = d / rel
        if p.is_symlink() or not p.is_file() or p.stat().st_size != rec["size"] or _sha256(p) != rec["sha256"]:
            raise WorkbenchError(f"bundle file failed verification: {rec['path']}")
    manifest["_dir"] = str(d)
    return manifest


def bundle_glb(manifest: dict) -> Path:
    files = {f["role"]: f["path"] for f in manifest["files"]}
    rel = files.get("working-glb") or files.get("source-glb")
    if rel is None:
        raise WorkbenchError("bundle has no GLB")
    return Path(manifest["_dir"]) / rel


def import_bundle(context, bundle_dir: str | Path, parent: bpy.types.Collection | None = None) -> bpy.types.Collection:
    """Import a verified bundle's GLB into a new collection, as a new candidate.

    Existing collections (including earlier revisions and artist edits) are never touched.
    On any failure every datablock created here is removed again.
    """
    manifest = load_bundle(bundle_dir)
    glb = bundle_glb(manifest)
    before = _ids_snapshot()
    scene = context.scene
    name = f"TW {manifest['display_name']} {manifest['asset_id'][:8]} r{manifest['revision']:04d}"
    with preserved_ui_state(context):
        try:
            coll = bpy.data.collections.new(name)
            (parent or scene.collection).children.link(coll)
            coll["tw_asset_id"] = manifest["asset_id"]
            coll["tw_revision"] = manifest["revision"]
            coll["tw_category"] = manifest["category"]
            coll["tw_status"] = manifest.get("status", "static")
            coll["tw_bundle"] = manifest["_dir"]
            lc = _find_layer_collection(context.view_layer.layer_collection, coll)
            context.view_layer.active_layer_collection = lc
            # The glTF importer converts glTF +Y-up to Blender +Z-up itself; nothing is rotated here.
            result = bpy.ops.import_scene.gltf(filepath=str(glb))
            if "FINISHED" not in result:
                raise WorkbenchError(f"glTF import failed: {result}")
            new_objects = [o for o in bpy.data.objects if o.as_pointer() not in before["objects"]]
            if not new_objects:
                raise WorkbenchError("glTF import created no objects")
            for o in new_objects:
                o["tw_asset_id"] = manifest["asset_id"]
                o["tw_revision"] = manifest["revision"]
                o["tw_role"] = "working"
                if o.name not in coll.objects:
                    for c in list(o.users_collection):
                        c.objects.unlink(o)
                    coll.objects.link(o)
        except BaseException:
            _remove_new_ids(before)
            raise
    return coll


def set_height(obj: bpy.types.Object, height_m: float) -> float:
    """Assign real-world scale to a working copy (generated output has none)."""
    _ensure_editable(obj)
    if height_m <= 0:
        raise WorkbenchError("height must be positive")
    world = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    current = max(v.z for v in world) - min(v.z for v in world)
    if current <= 0:
        raise WorkbenchError("object has no vertical extent")
    factor = height_m / current
    obj.scale = obj.scale * factor
    obj["tw_assigned_height_m"] = height_m
    return factor


# --- rigid accessories -------------------------------------------------------------------------


def similarity_transform(src: np.ndarray, dst: np.ndarray, allow_scale: bool = True) -> Matrix:
    """Umeyama least-squares similarity mapping src points onto dst points (3 or more each)."""
    if src.shape != dst.shape or src.shape[0] < 3:
        raise WorkbenchError("need at least three matching landmarks")
    mu_s, mu_d = src.mean(0), dst.mean(0)
    s, d = src - mu_s, dst - mu_d
    cov = d.T @ s / len(src)
    u, sig, vt = np.linalg.svd(cov)
    sign = np.eye(3)
    if np.linalg.det(u) * np.linalg.det(vt) < 0:
        sign[2, 2] = -1
    rot = u @ sign @ vt
    var = (s**2).sum() / len(src)
    if var < 1e-12:
        raise WorkbenchError("landmarks are degenerate (coincident)")
    scale = float(np.trace(np.diag(sig) @ sign) / var) if allow_scale else 1.0
    t = mu_d - scale * rot @ mu_s
    m = Matrix.Identity(4)
    for i in range(3):
        for j in range(3):
            m[i][j] = scale * rot[i, j]
        m[i][3] = t[i]
    return m


def align_by_landmarks(obj: bpy.types.Object, src_points, dst_points, allow_scale: bool = True) -> Matrix:
    _ensure_editable(obj)
    m = similarity_transform(np.asarray(src_points, float), np.asarray(dst_points, float), allow_scale)
    obj.matrix_world = m @ obj.matrix_world
    return m


def attach_rigid(obj: bpy.types.Object, target: bpy.types.Object, bone: str | None = None) -> None:
    """Parent an accessory to an object or bone, keeping its world placement. No weights,
    no Armature modifier: a rigid part should follow one bone, not deform."""
    _ensure_editable(obj)
    if bone and (target.type != "ARMATURE" or bone not in target.data.bones):
        raise WorkbenchError(f"{target.name} has no bone {bone!r}")
    world = obj.matrix_world.copy()
    obj.parent = target
    obj.parent_type = "BONE" if bone else "OBJECT"
    obj.parent_bone = bone or ""
    obj.matrix_parent_inverse = Matrix.Identity(4)
    bpy.context.view_layer.update()
    obj.matrix_world = world  # solves matrix_basis for the new parent
    obj["tw_attached_to"] = f"{target.name}:{bone}" if bone else target.name


# --- garments ----------------------------------------------------------------------------------


def body_armature(body: bpy.types.Object) -> bpy.types.Object:
    for mod in body.modifiers:
        if mod.type == "ARMATURE" and mod.object is not None:
            return mod.object
    raise WorkbenchError(f"{body.name} has no Armature modifier")


def deform_group_names(body: bpy.types.Object, arm: bpy.types.Object) -> list[str]:
    deform = {b.name for b in arm.data.bones if b.use_deform}
    return [g.name for g in body.vertex_groups if g.name in deform]


@contextmanager
def rest_pose(arm: bpy.types.Object):
    previous = arm.data.pose_position
    arm.data.pose_position = "REST"
    bpy.context.view_layer.update()
    try:
        yield
    finally:
        arm.data.pose_position = previous
        bpy.context.view_layer.update()


def garment_from_body(
    context,
    body: bpy.types.Object,
    face_indices,
    name: str | None = None,
    clearance: float = 0.004,
    thickness: float = 0.003,
) -> bpy.types.Object:
    """Path B: duplicate selected body faces into a separate garment with editable clearance and
    thickness. The body mesh itself is only read. Deform weights come along unchanged because
    the topology corresponds exactly."""
    import bmesh

    if body.type != "MESH":
        raise WorkbenchError("body must be a mesh")
    arm = body_armature(body)
    faces = {int(i) for i in face_indices}
    if not faces:
        raise WorkbenchError("select the body faces the garment should cover")
    before = _ids_snapshot()
    try:
        bm = bmesh.new()
        bm.from_mesh(body.data)  # rest-pose data; the body datablock is not modified
        bm.faces.ensure_lookup_table()
        bmesh.ops.delete(bm, geom=[f for f in bm.faces if f.index not in faces], context="FACES")
        loose = [v for v in bm.verts if not v.link_faces]
        bmesh.ops.delete(bm, geom=loose, context="VERTS")
        mesh = bpy.data.meshes.new(name or f"{body.name} Garment")
        bm.to_mesh(mesh)
        bm.free()
        for mat in body.data.materials:
            mesh.materials.append(mat)  # shared, unmodified
        garment = bpy.data.objects.new(mesh.name, mesh)
        for c in body.users_collection:
            c.objects.link(garment)
        garment.matrix_world = body.matrix_world.copy()
        keep = set(deform_group_names(body, arm))
        for g in body.vertex_groups:
            garment.vertex_groups.new(name=g.name)
        for g in [g for g in garment.vertex_groups if g.name not in keep]:
            garment.vertex_groups.remove(g)
        disp = garment.modifiers.new("TW Clearance", "DISPLACE")
        disp.direction = "NORMAL"
        disp.mid_level = 0.0
        disp.strength = clearance
        sol = garment.modifiers.new("TW Thickness", "SOLIDIFY")
        sol.thickness = thickness
        sol.offset = 1.0
        armature_mod = garment.modifiers.new("TW Armature", "ARMATURE")
        armature_mod.object = arm
        garment["tw_role"] = "garment"
        garment["tw_garment_path"] = "body-derived"
        garment["tw_source_body"] = body.name
        garment.parent = arm
        garment.matrix_parent_inverse = arm.matrix_world.inverted()
    except BaseException:
        _remove_new_ids(before)
        raise
    return garment


def proximity_fit_group(
    garment: bpy.types.Object, body: bpy.types.Object, fit_distance: float, group: str = "TW Fit"
) -> int:
    """Weight vertices near the rest-pose body for fitting; loose regions (skirts, coat tails)
    beyond `fit_distance` get zero weight and are left where they are."""
    depsgraph = bpy.context.evaluated_depsgraph_get()
    body_eval = body.evaluated_get(depsgraph)
    bvh = BVHTree.FromObject(body_eval, depsgraph)
    to_body = body.matrix_world.inverted() @ garment.matrix_world
    vg = garment.vertex_groups.get(group) or garment.vertex_groups.new(name=group)
    n = 0
    for v in garment.data.vertices:
        hit = bvh.find_nearest(to_body @ v.co)
        dist = hit[3] if hit[0] is not None else math.inf
        w = max(0.0, 1.0 - dist / fit_distance) if fit_distance > 0 else 0.0
        vg.add([v.index], w, "REPLACE")
        n += w > 0
    return n


def fit_generated_garment(
    context, garment: bpy.types.Object, body: bpy.types.Object, offset: float = 0.004, fit_distance: float = 0.05
) -> bpy.types.Object:
    """Path A: masked shrinkwrap of a generated garment, evaluated in rest pose and baked into a
    new working copy. The input garment object stays as it was."""
    _ensure_editable(garment)
    arm = body_armature(body)
    before = _ids_snapshot()
    try:
        with rest_pose(arm):
            work = garment.copy()
            work.data = garment.data.copy()
            work.name = f"{garment.name} Fitted"
            for c in garment.users_collection:
                c.objects.link(work)
            work.modifiers.clear()
            fitted = proximity_fit_group(work, body, fit_distance)
            sw = work.modifiers.new("TW Fit", "SHRINKWRAP")
            sw.target = body
            sw.wrap_method = "NEAREST_SURFACEPOINT"
            sw.wrap_mode = "OUTSIDE_SURFACE"
            sw.offset = offset
            sw.vertex_group = "TW Fit"
            _apply_modifier(work, sw.name)
        work["tw_role"] = "garment"
        work["tw_garment_path"] = "generated-fit"
        work["tw_fitted_vertices"] = fitted
        work["tw_note"] = "Fitting aid only: check construction, then retopologize before deformation use."
    except BaseException:
        _remove_new_ids(before)
        raise
    return work


def _apply_modifier(obj: bpy.types.Object, name: str) -> None:
    with bpy.context.temp_override(
        object=obj, active_object=obj, selected_objects=[obj], selected_editable_objects=[obj]
    ):
        result = bpy.ops.object.modifier_apply(modifier=name)
    if "FINISHED" not in result:
        raise WorkbenchError(f"applying {name} failed: {result}")


def transfer_weights(garment: bpy.types.Object, body: bpy.types.Object, max_distance: float = 0.1) -> dict:
    """Initial binding: copy deform-bone weights from the rest-pose body surface, normalize,
    attach to the existing armature (unchanged) and report problems. Not a quality certificate."""
    _ensure_editable(garment)
    arm = body_armature(body)
    names = deform_group_names(body, arm)
    if not names:
        raise WorkbenchError("body has no deform-bone vertex groups")
    for g in [g for g in garment.vertex_groups if g.name in names]:
        garment.vertex_groups.remove(g)
    for n in names:
        garment.vertex_groups.new(name=n)
    with rest_pose(arm):
        dt = garment.modifiers.new("TW Weight Transfer", "DATA_TRANSFER")
        dt.object = body
        dt.use_object_transform = True
        dt.use_vert_data = True
        dt.data_types_verts = {"VGROUP_WEIGHTS"}
        dt.vert_mapping = "POLYINTERP_NEAREST"
        # Source "ALL" + destination "NAME": only the deform-bone groups created above receive
        # weights (a DataTransfer modifier never creates groups, so non-deform groups are skipped).
        dt.layers_vgroup_select_src = "ALL"
        dt.layers_vgroup_select_dst = "NAME"
        dt.use_max_distance = True
        dt.max_distance = max_distance
        # Apply against the undeformed garment: the transfer must be first in the stack.
        garment.modifiers.move(len(garment.modifiers) - 1, 0)
        bpy.context.view_layer.update()
        _apply_modifier(garment, dt.name)
    report = normalize_and_report(garment, arm, names)
    if not any(m.type == "ARMATURE" and m.object == arm for m in garment.modifiers):
        mod = garment.modifiers.new("TW Armature", "ARMATURE")
        mod.object = arm
    garment["tw_binding"] = json.dumps(report)
    return report


def _side(name: str) -> str | None:
    m = SIDE_RE.search(name)
    if not m:
        return None
    s = (m.group("a") or m.group("b") or m.group("c")).upper()
    return "L" if s.startswith("L") else "R"


def normalize_and_report(
    obj: bpy.types.Object, arm: bpy.types.Object, names: list[str], side_tolerance: float = 0.02
) -> dict:
    idx = {obj.vertex_groups[n].index: n for n in names if n in obj.vertex_groups}
    unweighted = 0
    cross = 0
    to_arm = arm.matrix_world.inverted() @ obj.matrix_world
    for v in obj.data.vertices:
        ws = [(g.group, g.weight) for g in v.groups if g.group in idx]
        total = sum(w for _, w in ws)
        if total <= 1e-6:
            unweighted += 1
            continue
        for gi, w in ws:
            obj.vertex_groups[gi].add([v.index], w / total, "REPLACE")
        x = (to_arm @ v.co).x
        for gi, w in ws:
            side = _side(idx[gi])
            # Blender rigs face -Y: the character's left side is +X.
            if (
                side
                and w / total > 0.1
                and ((side == "L" and x < -side_tolerance) or (side == "R" and x > side_tolerance))
            ):
                cross += 1
                break
    return {
        "vertices": len(obj.data.vertices),
        "unweighted": unweighted,
        "cross_side": cross,
        "groups": len(idx),
        "note": "initial binding only; inspect deformation in several poses",
    }


# --- diagnostics -------------------------------------------------------------------------------


def penetration_count(garment: bpy.types.Object, body: bpy.types.Object) -> dict:
    """Advisory count of evaluated garment vertices lying inside the evaluated body surface at
    the current pose (nearest-face normal test, not an exact collision certificate)."""
    depsgraph = bpy.context.evaluated_depsgraph_get()
    b_eval = body.evaluated_get(depsgraph)
    bvh = BVHTree.FromObject(b_eval, depsgraph)
    g_eval = garment.evaluated_get(depsgraph)
    mesh = g_eval.to_mesh()
    to_body = body.matrix_world.inverted() @ garment.matrix_world
    inside = 0
    try:
        for v in mesh.vertices:
            p = to_body @ v.co
            loc, normal, _i, _d = bvh.find_nearest(p)
            if loc is not None and (p - loc).dot(normal) < 0:
                inside += 1
        total = len(mesh.vertices)
    finally:
        g_eval.to_mesh_clear()
    return {"inside": inside, "vertices": total, "advisory": True}


def inspection_poses(arm: bpy.types.Object, angle_deg: float = 35.0):
    """Yield (label) after posing every deform bone through a few rotations, then restore the
    original pose exactly. The armature datablock and its actions are never edited."""
    saved = {
        pb.name: (
            pb.rotation_mode,
            pb.rotation_quaternion.copy(),
            pb.rotation_euler.copy(),
            pb.location.copy(),
            pb.scale.copy(),
        )
        for pb in arm.pose.bones
    }
    anim = arm.animation_data
    saved_action = anim.action if anim else None
    if anim:
        anim.action = None  # evaluate our temporary pose, not the action; restored below
    try:
        for label, axis in (("rest", None), ("flex+x", "X"), ("flex-x", "-X"), ("twist+z", "Z")):
            for pb in arm.pose.bones:
                pb.rotation_mode = "XYZ"
                pb.rotation_euler = (0, 0, 0)
                if axis and pb.bone.use_deform and pb.parent is not None:
                    sign = -1 if axis.startswith("-") else 1
                    pb.rotation_euler["XYZ".index(axis[-1])] = sign * math.radians(angle_deg)
            bpy.context.view_layer.update()
            yield label
    finally:
        for pb in arm.pose.bones:
            mode, q, e, loc, sc = saved[pb.name]
            pb.rotation_mode = mode
            pb.rotation_quaternion, pb.rotation_euler, pb.location, pb.scale = q, e, loc, sc
        if anim:
            anim.action = saved_action
        bpy.context.view_layer.update()


def status_label(obj: bpy.types.Object) -> str:
    """Integration readiness from what has actually been checked, not from the preset."""
    if obj.get("tw_deformation_validated"):
        return "deformation-validated"
    if obj.get("tw_role") == "garment" or obj.get("tw_binding"):
        return "retopology-needed"
    return "static"


def helper_command() -> list[str]:
    """The helper binary from the Home Manager config (no PATH guessing inside Blender)."""
    import tomllib

    cfg = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "trellis-workbench/config.toml"
    if cfg.is_file():
        helper = tomllib.loads(cfg.read_text()).get("helper")
        if helper:
            return [helper, "serve-addon"]
    return ["trellis-workbench", "serve-addon"]
