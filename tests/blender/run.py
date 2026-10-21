"""Headless Blender acceptance tests (no models, no GPU).

    blender --background --factory-startup --python-exit-code 1 --python tests/blender/run.py -- FIXTURES.json OUT_DIR

Each test builds a procedural rigged body scene with unrelated data, runs the add-on's real
operations, and asserts the protected datablocks are bit-for-bit unchanged.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import traceback
from pathlib import Path

import bpy
import numpy as np
from mathutils import Matrix, Vector

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "blender_addon"))
from trellis_workbench import core

ARGS = sys.argv[sys.argv.index("--") + 1 :]
FIXTURES = json.loads(Path(ARGS[0]).read_text())
OUT = Path(ARGS[1])
OUT.mkdir(parents=True, exist_ok=True)
RESULTS: list[tuple[str, str, str]] = []


# --- fixture scene -----------------------------------------------------------------------------


def build_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.frame_start, scene.frame_end = 1, 48
    scene.render.fps = 24
    scene.unit_settings.scale_length = 1.0

    arm_data = bpy.data.armatures.new("Rig")
    arm = bpy.data.objects.new("Rig", arm_data)
    scene.collection.objects.link(arm)
    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode="EDIT")
    eb = arm_data.edit_bones
    root = eb.new("root")
    root.head, root.tail, root.use_deform = (0, 0, 0), (0, 0, 0.3), False
    spine = eb.new("spine")
    spine.head, spine.tail, spine.parent = (0, 0, 0.8), (0, 0, 1.4), root
    for side, sx in (("L", 1), ("R", -1)):
        b = eb.new(f"upper_arm.{side}")
        b.head, b.tail, b.parent = (0.2 * sx, 0, 1.35), (0.6 * sx, 0, 1.35), spine
    bpy.ops.object.mode_set(mode="OBJECT")

    # torso cylinder + two arm cylinders, joined into one body mesh
    bpy.ops.mesh.primitive_cylinder_add(vertices=24, radius=0.15, depth=0.8, location=(0, 0, 1.0))
    torso = bpy.context.active_object
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.subdivide(number_cuts=6)
    bpy.ops.object.mode_set(mode="OBJECT")
    parts = [torso]
    for sx in (1, -1):
        bpy.ops.mesh.primitive_cylinder_add(
            vertices=12, radius=0.05, depth=0.45, location=(0.38 * sx, 0, 1.35), rotation=(0, math.pi / 2, 0)
        )
        parts.append(bpy.context.active_object)
    with bpy.context.temp_override(active_object=torso, selected_editable_objects=parts):
        bpy.ops.object.join()
    body = torso
    body.name = body.data.name = "Body"
    body.data.transform(body.matrix_world)  # bake the transform: rest-pose data in rig space
    body.matrix_world = Matrix.Identity(4)
    skin = bpy.data.materials.new("Skin")
    skin.diffuse_color = (0.8, 0.6, 0.5, 1)
    body.data.materials.append(skin)
    groups = {n: body.vertex_groups.new(name=n) for n in ("spine", "upper_arm.L", "upper_arm.R", "not_a_bone")}
    for v in body.data.vertices:
        x = v.co.x
        if x > 0.17:
            groups["upper_arm.L"].add([v.index], 1.0, "REPLACE")
        elif x < -0.17:
            groups["upper_arm.R"].add([v.index], 1.0, "REPLACE")
        else:
            groups["spine"].add([v.index], 1.0, "REPLACE")
        groups["not_a_bone"].add([v.index], 0.5, "REPLACE")
    mod = body.modifiers.new("Armature", "ARMATURE")
    mod.object = arm
    body.parent = arm

    action = bpy.data.actions.new("Wave")
    arm.animation_data_create().action = action
    pb = arm.pose.bones["upper_arm.L"]
    pb.rotation_mode = "XYZ"
    for frame, angle in ((1, 0.0), (20, 1.0), (40, -0.6)):
        pb.rotation_euler = (0, angle, 0)
        pb.keyframe_insert("rotation_euler", frame=frame)
    pb.rotation_euler = (0, 0, 0)

    bpy.ops.mesh.primitive_cube_add(size=0.3, location=(1.5, 0, 0.15))
    other = bpy.context.active_object
    other.name = "Unrelated"
    mat = bpy.data.materials.new("UnrelatedMat")
    mat.use_nodes = True
    mat.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (0.1, 0.7, 0.2, 1)
    other.data.materials.append(mat)
    cam = bpy.data.objects.new("Camera", bpy.data.cameras.new("Camera"))
    scene.collection.objects.link(cam)
    cam.location, cam.rotation_euler = (0, -4, 1.2), (math.radians(88), 0, 0)
    scene.camera = cam
    light = bpy.data.objects.new("Sun", bpy.data.lights.new("Sun", "SUN"))
    scene.collection.objects.link(light)
    scene.frame_set(1)
    for o in bpy.context.view_layer.objects:
        o.select_set(o.name == "Unrelated")
    bpy.context.view_layer.objects.active = other
    return body, arm, other


def action_fcurves(action):
    """F-curves of a Blender 4.4+ layered action (slots/channelbags) or a legacy one."""
    if not getattr(action, "is_action_layered", False):
        return list(action.fcurves)
    return [fc for layer in action.layers for strip in layer.strips for cb in strip.channelbags for fc in cb.fcurves]


def fingerprint() -> str:
    """Hash of everything the add-on must never change."""
    h = hashlib.sha256()
    body = bpy.data.objects["Body"]
    co = np.empty(len(body.data.vertices) * 3)
    body.data.vertices.foreach_get("co", co)
    h.update(co.tobytes())
    for v in body.data.vertices:
        h.update(repr(sorted((g.group, round(g.weight, 7)) for g in v.groups)).encode())
    h.update(repr([m.name for m in body.data.materials] + [(m.type, m.name) for m in body.modifiers]).encode())
    h.update(repr([g.name for g in body.vertex_groups]).encode())
    h.update(repr(tuple(body.matrix_world)).encode())
    rig = bpy.data.objects["Rig"]
    for b in rig.data.bones:
        h.update(
            repr(
                (
                    b.name,
                    tuple(b.head_local),
                    tuple(b.tail_local),
                    tuple(map(tuple, b.matrix_local)),
                    b.use_deform,
                    b.parent.name if b.parent else None,
                )
            ).encode()
        )
    h.update(repr((rig.data.pose_position, rig.animation_data.action.name)).encode())
    for fc in action_fcurves(bpy.data.actions["Wave"]):
        h.update(repr((fc.data_path, fc.array_index, [tuple(k.co) for k in fc.keyframe_points])).encode())
    other = bpy.data.objects["Unrelated"]
    h.update(repr((tuple(other.matrix_world), len(other.data.vertices))).encode())
    nt = bpy.data.materials["UnrelatedMat"].node_tree
    h.update(repr(tuple(nt.nodes["Principled BSDF"].inputs["Base Color"].default_value)).encode())
    h.update(repr(tuple(bpy.data.materials["Skin"].diffuse_color)).encode())
    s = bpy.context.scene
    h.update(
        repr(
            (
                s.frame_start,
                s.frame_end,
                s.render.fps,
                s.render.engine,
                s.unit_settings.scale_length,
                s.camera.name,
                tuple(s.camera.matrix_world),
                bpy.data.lights["Sun"].energy,
            )
        ).encode()
    )
    return h.hexdigest()


def ui_state():
    vl = bpy.context.view_layer
    return (
        sorted(o.name for o in bpy.context.selected_objects),
        vl.objects.active.name if vl.objects.active else None,
        bpy.context.mode,
        vl.active_layer_collection.name,
    )


def test(fn):
    name = fn.__name__
    try:
        result = fn()
        RESULTS.append(
            (name, "skipped" if isinstance(result, str) and result.startswith("SKIP") else "passed", result or "")
        )
    except Exception:  # noqa: BLE001
        RESULTS.append((name, "failed", traceback.format_exc()))
    return fn


def world_dims(objs):
    pts = [o.matrix_world @ Vector(c) for o in objs if o.type == "MESH" for c in o.bound_box]
    return [max(p[i] for p in pts) - min(p[i] for p in pts) for i in range(3)]


def mesh_objects(coll):
    return [o for o in coll.all_objects if o.type == "MESH"]


# --- tests -------------------------------------------------------------------------------------


@test
def import_preserves_scene_and_converts_axes_once():
    build_scene()
    before, state = fingerprint(), ui_state()
    coll = core.import_bundle(bpy.context, FIXTURES["probe"])
    assert fingerprint() == before, "protected data changed"
    assert ui_state() == state, f"UI state not restored: {ui_state()} vs {state}"
    assert coll["tw_revision"] == 1 and len(coll["tw_asset_id"]) == 32
    objs = mesh_objects(coll)
    assert objs and all(o.get("tw_asset_id") == coll["tw_asset_id"] for o in objs)
    dx, dy, dz = world_dims(objs)
    # glTF box extents (x=1, y(up)=2, z=0.5) must land as Blender (x=1, y=0.5, z(up)=2): one conversion only.
    assert abs(dx - 1.0) < 1e-4 and abs(dy - 0.5) < 1e-4 and abs(dz - 2.0) < 1e-4, (dx, dy, dz)


@test
def pbr_channels_are_wired_by_convention():
    build_scene()
    coll = core.import_bundle(bpy.context, FIXTURES["probe"])
    mat = mesh_objects(coll)[0].active_material
    nt = mat.node_tree
    bsdf = next(n for n in nt.nodes if n.type == "BSDF_PRINCIPLED")

    def source(socket):
        link = socket.links[0] if socket.is_linked else None
        return link.from_node if link else None

    base_img = source(bsdf.inputs["Base Color"])
    assert base_img.type == "TEX_IMAGE" and base_img.image.colorspace_settings.name == "sRGB"
    rough_sep = source(bsdf.inputs["Roughness"])
    metal_sep = source(bsdf.inputs["Metallic"])
    assert rough_sep == metal_sep and rough_sep.type in {"SEPARATE_COLOR", "SEPRGB"}
    assert bsdf.inputs["Roughness"].links[0].from_socket.name in {"Green", "G"}
    assert bsdf.inputs["Metallic"].links[0].from_socket.name in {"Blue", "B"}
    orm = source(rough_sep.inputs[0])
    assert orm.image.colorspace_settings.name == "Non-Color"
    px = np.array(orm.image.pixels[:4])
    assert abs(px[1] - 64 / 255) < 0.01 and abs(px[2] - 191 / 255) < 0.01, px  # distinctive channel values
    nmap = source(bsdf.inputs["Normal"])
    assert nmap.type == "NORMAL_MAP" and nmap.space == "TANGENT"
    assert source(nmap.inputs["Color"]).image.colorspace_settings.name == "Non-Color"
    assert not mat.use_backface_culling  # doubleSided
    assert bsdf.inputs["Alpha"].is_linked  # alphaMode MASK keeps an alpha path


@test
def failed_import_rolls_back():
    build_scene()
    before = fingerprint()
    counts = {n: len(getattr(bpy.data, n)) for n in core.ID_COLLECTIONS}
    state = ui_state()
    try:
        core.import_bundle(bpy.context, FIXTURES["broken"])
    except Exception as exc:  # noqa: BLE001
        print(f"broken GLB refused: {exc}")
    else:
        raise AssertionError("broken GLB imported")
    assert {n: len(getattr(bpy.data, n)) for n in core.ID_COLLECTIONS} == counts
    assert fingerprint() == before and ui_state() == state
    # tampered bundle: refused before anything is created
    tampered = OUT / "tampered"
    import shutil

    shutil.copytree(FIXTURES["probe"], tampered, dirs_exist_ok=True)
    (tampered / "working/model.glb").write_bytes(b"x" + (tampered / "working/model.glb").read_bytes()[1:])
    try:
        core.import_bundle(bpy.context, tampered)
        raise AssertionError("tampered bundle accepted")
    except core.WorkbenchError:
        pass
    assert {n: len(getattr(bpy.data, n)) for n in core.ID_COLLECTIONS} == counts


@test
def new_revision_never_overwrites_edits():
    build_scene()
    r1 = core.import_bundle(bpy.context, FIXTURES["probe"])
    edited = mesh_objects(r1)[0]
    edited.location = (3, 2, 1)
    edited.data.vertices[0].co.x += 0.25
    r2 = core.import_bundle(bpy.context, FIXTURES["probe_r2"])
    assert r1.name != r2.name and r2["tw_revision"] == 2
    assert tuple(edited.location) == (3, 2, 1) and edited.name in bpy.data.objects
    assert abs(world_dims(mesh_objects(r2))[1] - 0.6) < 1e-4


@test
def rigid_accessory_follows_bone_without_weights():
    _body, arm, _ = build_scene()
    before = fingerprint()
    coll = core.import_bundle(bpy.context, FIXTURES["plate"])
    plate = mesh_objects(coll)[0]
    # landmark alignment recovers a known similarity transform
    src = np.array([[0, 0, 0], [0.1, 0, 0], [0, 0.1, 0], [0, 0, 0.1]])
    rot = Matrix.Rotation(0.7, 3, "Z") @ Matrix.Rotation(0.3, 3, "X")
    dst = np.array([(rot @ Vector(p)) * 1.5 + Vector((0.4, 0, 1.4)) for p in src])
    m = core.similarity_transform(src, dst)
    assert abs(m.to_scale()[0] - 1.5) < 1e-6 and (m.translation - Vector((0.4, 0, 1.4))).length < 1e-6
    core.align_by_landmarks(plate, src, dst)
    world0 = plate.matrix_world.copy()
    core.attach_rigid(plate, arm, "upper_arm.L")
    assert (plate.matrix_world.translation - world0.translation).length < 1e-5, "placement changed on attach"
    assert not plate.vertex_groups and not any(md.type == "ARMATURE" for md in plate.modifiers)
    pb = arm.pose.bones["upper_arm.L"]
    rel = []
    for frame in (1, 20, 40):
        bpy.context.scene.frame_set(frame)
        rel.append((arm.matrix_world @ pb.matrix).inverted() @ plate.matrix_world)
    for r in rel[1:]:
        assert all(abs(a - b) < 1e-4 for ra, rb in zip(r, rel[0]) for a, b in zip(ra, rb)), "accessory slid on bone"
    bpy.context.scene.frame_set(1)
    assert fingerprint() == before
    # the 1 mm plate survived import intact (nothing thickened or remeshed it)
    assert abs(min(plate.dimensions) - 0.001 * 1.5) < 1e-5, tuple(plate.dimensions)


@test
def garment_from_body_is_separate_and_bound():
    body, arm, _ = build_scene()
    before = fingerprint()
    faces = [p.index for p in body.data.polygons if 0.75 < p.center.z < 1.25 and abs(p.center.x) < 0.16]
    garment = core.garment_from_body(bpy.context, body, faces, clearance=0.01, thickness=0.004)
    assert fingerprint() == before, "body changed"
    assert garment.data is not body.data and garment.name != body.name
    names = {g.name for g in garment.vertex_groups}
    assert names == {"spine", "upper_arm.L", "upper_arm.R"}, names
    assert any(md.type == "ARMATURE" and md.object == arm for md in garment.modifiers)
    # openings preserved: the tube keeps at least its neck and waist boundary loops
    import bmesh

    bm = bmesh.new()
    bm.from_mesh(garment.data)
    boundary = [e for e in bm.edges if e.is_boundary]
    assert len(boundary) >= 2 * 24, len(boundary)
    bm.free()
    rep = core.normalize_and_report(garment, arm, ["spine", "upper_arm.L", "upper_arm.R"])
    assert rep["unweighted"] == 0 and rep["cross_side"] == 0, rep
    results = {label: core.penetration_count(garment, body)["inside"] for label in core.inspection_poses(arm)}
    assert results["rest"] == 0, results
    assert fingerprint() == before, "inspection poses leaked into the rig"


@test
def generated_garment_fit_and_weight_transfer():
    body, arm, _ = build_scene()
    before = fingerprint()
    coll = core.import_bundle(bpy.context, FIXTURES["sleeve"])
    sleeve = mesh_objects(coll)[0]
    sleeve.location = (0, 0, 0.8)  # 0.5 m tube around the torso (z 0.8..1.3), loose flared hem at the bottom
    bpy.context.view_layer.update()
    orig_co = [v.co.copy() for v in sleeve.data.vertices]
    hem_before = max(Vector((v.co.x, v.co.y)).length for v in sleeve.data.vertices if v.co.z < 1e-4)
    fitted = core.fit_generated_garment(bpy.context, sleeve, body, offset=0.005, fit_distance=0.05)
    assert [v.co for v in sleeve.data.vertices] == orig_co, "source garment modified"
    # torso band away from the arm junction (where snapping to the arm surface is correct)
    near = [Vector((v.co.x, v.co.y)).length for v in fitted.data.vertices if 0.15 < v.co.z < 0.35]
    assert max(near) < 0.165, f"fitted region not pulled onto the body: {max(near)}"
    hem_after = max(Vector((v.co.x, v.co.y)).length for v in fitted.data.vertices if v.co.z < 1e-4)
    assert abs(hem_after - hem_before) < 1e-5, "loose hem collapsed onto the body"
    rep = core.transfer_weights(fitted, body, max_distance=0.4)
    assert rep["unweighted"] == 0 and rep["groups"] == 3, rep
    assert any(md.type == "ARMATURE" and md.object == arm for md in fitted.modifiers)
    assert fingerprint() == before


@test
def undo_restores_scene():
    build_scene()
    try:
        bpy.ops.ed.undo_push(message="before import")
    except RuntimeError as exc:
        return f"SKIP undo stack unavailable in --background: {exc}"
    n = len(bpy.data.objects)
    core.import_bundle(bpy.context, FIXTURES["probe"])
    bpy.ops.ed.undo_push(message="import")
    try:
        bpy.ops.ed.undo()
    except RuntimeError as exc:
        return f"SKIP undo unavailable in --background: {exc}"
    assert len(bpy.data.objects) == n


@test
def reopen_keeps_textures_and_attachments():
    body, arm, _ = build_scene()
    coll = core.import_bundle(bpy.context, FIXTURES["probe"])
    acc = mesh_objects(coll)[0]
    core.attach_rigid(acc, arm, "spine")
    faces = [p.index for p in body.data.polygons if 0.9 < p.center.z < 1.1 and abs(p.center.x) < 0.16]
    core.garment_from_body(bpy.context, body, faces)
    asset_id = coll["tw_asset_id"]
    bpy.ops.file.pack_all()
    path = OUT / "project.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(path))
    before = fingerprint()
    bpy.ops.wm.open_mainfile(filepath=str(path))
    assert fingerprint() == before
    acc = next(o for o in bpy.data.objects if o.get("tw_asset_id") == asset_id and o.type == "MESH")
    assert acc.parent.name == "Rig" and acc.parent_type == "BONE" and acc.parent_bone == "spine"
    imgs = [i for i in bpy.data.images if i.source == "FILE"]
    assert imgs and all(i.packed_file is not None and i.has_data or i.pixels[:1] for i in imgs)
    garment = next(o for o in bpy.data.objects if o.get("tw_role") == "garment")
    assert any(md.type == "ARMATURE" for md in garment.modifiers)


@test
def cycles_render_shows_asset():
    build_scene()
    coll = core.import_bundle(bpy.context, FIXTURES["probe"])
    for o in mesh_objects(coll):
        o.location = (0, 0, 1.0)
    s = bpy.context.scene
    s.render.engine = "CYCLES"
    s.cycles.device = "CPU"
    s.cycles.samples = 4
    s.render.resolution_x = s.render.resolution_y = 48
    s.render.filepath = str(OUT / "probe.png")
    if s.world is None:
        s.world = bpy.data.worlds.new("World")
    bpy.ops.render.render(write_still=True)
    img = bpy.data.images.load(str(OUT / "probe.png"))
    px = np.array(img.pixels[:]).reshape(48, 48, 4)
    center = px[20:28, 20:28, :3].mean(axis=(0, 1))
    # base colour (200, 40, 20) sRGB: the red channel must dominate where the probe is
    assert center[0] > center[1] * 1.5 and center[0] > center[2] * 1.5, center


@test
def addon_registers_from_system_repository():
    repo = os.environ.get("BLENDER_SYSTEM_EXTENSIONS")
    if not repo:
        return "SKIP BLENDER_SYSTEM_EXTENSIONS not set"
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.extensions.repo_refresh_all()
    module = "bl_ext.system.trellis_workbench"
    bpy.ops.preferences.addon_enable(module=module)
    assert hasattr(bpy.types, "TW_PT_panel") and hasattr(bpy.context.scene, "trellis_workbench")
    bpy.ops.preferences.addon_disable(module=module)
    assert not hasattr(bpy.types, "TW_PT_panel")


summary = {"results": [{"name": n, "status": s, "detail": d} for n, s, d in RESULTS]}
(OUT / "blender-results.json").write_text(json.dumps(summary, indent=2))
for n, s, d in RESULTS:
    print(f"{s.upper():8} {n}" + (f"\n{d}" if s != "passed" and d else ""))
if any(s == "failed" for _n, s, _d in RESULTS):
    raise SystemExit(1)
