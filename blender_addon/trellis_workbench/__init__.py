"""TRELLIS Workbench: generate, inspect, import and fit TRELLIS.2 assets in the open project.

Network and generation run in the external `trellis-workbench serve-addon` helper. This add-on
only exchanges JSON lines with it from a timer on Blender's main thread.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, StringProperty

from . import core

MODES = [(m, m.title(), "") for m in ("accessory", "garment", "human", "animal")]
PROFILES = [(p, p, "") for p in ("standard", "low-memory", "high")]


# --- helper process ----------------------------------------------------------------------------


class Helper:
    """One helper subprocess per Blender session; replies are polled by a timer."""

    proc: subprocess.Popen | None = None
    replies: list[dict] = []
    log: list[str] = []

    @classmethod
    def ensure(cls) -> None:
        if cls.proc is None or cls.proc.poll() is not None:
            import os

            cls.proc = subprocess.Popen(
                core.helper_command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1
            )
            os.set_blocking(cls.proc.stdout.fileno(), False)
            if not bpy.app.timers.is_registered(poll_helper):
                bpy.app.timers.register(poll_helper, first_interval=0.5, persistent=True)

    @classmethod
    def send(cls, op: str, **args) -> str:
        cls.ensure()
        rid = uuid.uuid4().hex
        cls.proc.stdin.write(json.dumps({"schema_version": 1, "id": rid, "op": op, "args": args}) + "\n")
        cls.proc.stdin.flush()
        return rid

    @classmethod
    def stop(cls) -> None:
        if cls.proc and cls.proc.poll() is None:
            try:
                cls.proc.stdin.write(json.dumps({"schema_version": 1, "id": "bye", "op": "shutdown"}) + "\n")
                cls.proc.stdin.flush()
                cls.proc.wait(timeout=3)
            except Exception:  # noqa: BLE001
                cls.proc.terminate()
        cls.proc = None


def poll_helper():
    p = Helper.proc
    if p is None:
        return None
    while True:
        try:
            line = p.stdout.readline()
        except (OSError, ValueError):
            break
        if not line:
            break
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        Helper.replies.append(msg)
        data = msg.get("data", {})
        text = data.get("message") or data.get("event") or data.get("state") or ""
        if text:
            Helper.log = (
                Helper.log + [f"{'ERR ' if not msg.get('ok') else ''}{text} {data.get('detail', '')}".strip()]
            )[-8:]
    for area in bpy.context.window_manager.windows[0].screen.areas if bpy.context.window_manager.windows else []:
        if area.type == "VIEW_3D":
            area.tag_redraw()
    return 1.0 if p.poll() is None else None


# --- properties --------------------------------------------------------------------------------


class TW_Props(bpy.types.PropertyGroup):
    reference: StringProperty(name="Reference", subtype="FILE_PATH")
    mask: StringProperty(name="Mask (optional)", subtype="FILE_PATH")
    mode: EnumProperty(name="Mode", items=MODES)
    profile: EnumProperty(name="Quality", items=PROFILES)
    backend: StringProperty(name="Backend", description="Configured backend name (empty = default)")
    shape_only: BoolProperty(name="Shape only", default=False)
    bundle: StringProperty(name="Bundle", subtype="DIR_PATH")
    height: FloatProperty(name="Height (m)", default=1.0, min=0.001)
    bone: StringProperty(name="Bone")
    allow_scale: BoolProperty(name="Allow uniform scale", default=True)
    clearance: FloatProperty(name="Clearance (m)", default=0.004, min=0.0, precision=4)
    thickness: FloatProperty(name="Thickness (m)", default=0.003, min=0.0, precision=4)
    fit_distance: FloatProperty(name="Fit distance (m)", default=0.05, min=0.0, precision=3)
    transfer_distance: FloatProperty(name="Transfer max distance (m)", default=0.1, min=0.0, precision=3)
    report: StringProperty(name="Last report")


def _props(context) -> TW_Props:
    return context.scene.trellis_workbench


# --- operators ---------------------------------------------------------------------------------


class TW_OT_generate(bpy.types.Operator):
    bl_idname = "trellis_workbench.generate"
    bl_label = "Generate"
    bl_description = "Send the reference to the TRELLIS.2 backend; the result appears as a new bundle"

    def execute(self, context):
        p = _props(context)
        if not p.reference:
            self.report({"ERROR"}, "choose a reference image")
            return {"CANCELLED"}
        Helper.send(
            "submit",
            image=bpy.path.abspath(p.reference),
            mask=bpy.path.abspath(p.mask) if p.mask else None,
            mode=p.mode,
            profile=p.profile,
            backend=p.backend or None,
            stage="shape" if p.shape_only else "full",
            name=Path(p.reference).stem,
        )
        self.report({"INFO"}, "submitted; progress appears in the panel")
        return {"FINISHED"}


class TW_OT_refresh(bpy.types.Operator):
    bl_idname = "trellis_workbench.refresh"
    bl_label = "Refresh"

    def execute(self, context):
        Helper.send("status")
        Helper.send("list_bundles")
        return {"FINISHED"}


class TW_OT_cancel(bpy.types.Operator):
    bl_idname = "trellis_workbench.cancel"
    bl_label = "Cancel job"
    job_id: StringProperty()

    def execute(self, context):
        Helper.send("cancel", job_id=self.job_id)
        return {"FINISHED"}


class TW_OT_import(bpy.types.Operator):
    bl_idname = "trellis_workbench.import_bundle"
    bl_label = "Import revision"
    bl_description = "Import a verified bundle into a new collection; nothing existing is replaced"
    bl_options = {"REGISTER", "UNDO"}
    path: StringProperty(subtype="DIR_PATH")

    def execute(self, context):
        path = self.path or bpy.path.abspath(_props(context).bundle)
        try:
            coll = core.import_bundle(context, path)
        except (core.WorkbenchError, OSError, ValueError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        self.report({"INFO"}, f"imported into {coll.name}")
        return {"FINISHED"}


class TW_OT_set_height(bpy.types.Operator):
    bl_idname = "trellis_workbench.set_height"
    bl_label = "Set real-world height"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return context.active_object is not None

    def execute(self, context):
        try:
            core.set_height(context.active_object, _props(context).height)
        except core.WorkbenchError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


def _landmarks(prefix: str):
    pts = sorted((o for o in bpy.data.objects if o.name.startswith(prefix) and o.type == "EMPTY"), key=lambda o: o.name)
    return [tuple(o.matrix_world.translation) for o in pts]


class TW_OT_add_landmarks(bpy.types.Operator):
    bl_idname = "trellis_workbench.add_landmarks"
    bl_label = "Add landmark pairs"
    bl_description = "Create 3 asset (TW_A*) and 3 target (TW_T*) empties at the 3D cursor to place by hand"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        cur = context.scene.cursor.location
        for prefix in ("TW_A", "TW_T"):
            for i in range(1, 4):
                name = f"{prefix}{i}"
                if name in bpy.data.objects:
                    continue
                e = bpy.data.objects.new(name, None)
                e.empty_display_type = "SPHERE" if prefix == "TW_A" else "CUBE"
                e.empty_display_size = 0.02
                e.location = cur
                context.collection.objects.link(e)
        return {"FINISHED"}


class TW_OT_align(bpy.types.Operator):
    bl_idname = "trellis_workbench.align"
    bl_label = "Align by landmarks"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return context.active_object is not None

    def execute(self, context):
        try:
            core.align_by_landmarks(
                context.active_object, _landmarks("TW_A"), _landmarks("TW_T"), _props(context).allow_scale
            )
        except core.WorkbenchError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


class TW_OT_attach(bpy.types.Operator):
    bl_idname = "trellis_workbench.attach"
    bl_label = "Attach to target"
    bl_description = "Parent the active accessory to the other selected object (or its bone), keeping placement"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        obj = context.active_object
        targets = [o for o in context.selected_objects if o != obj]
        if obj is None or len(targets) != 1:
            self.report({"ERROR"}, "select the target, then the accessory (active)")
            return {"CANCELLED"}
        try:
            core.attach_rigid(obj, targets[0], _props(context).bone or None)
        except core.WorkbenchError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


class TW_OT_garment_from_body(bpy.types.Operator):
    bl_idname = "trellis_workbench.garment_from_body"
    bl_label = "Garment from selected body faces"
    bl_description = "Duplicate the selected faces of the active body into a separate, bound garment"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        body = context.active_object
        if body is None or body.type != "MESH":
            self.report({"ERROR"}, "make the body mesh active and select faces in Edit Mode")
            return {"CANCELLED"}
        if context.mode == "EDIT_MESH":
            bpy.ops.object.mode_set(mode="OBJECT")
        faces = [p.index for p in body.data.polygons if p.select]
        p = _props(context)
        try:
            g = core.garment_from_body(context, body, faces, clearance=p.clearance, thickness=p.thickness)
        except core.WorkbenchError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        self.report({"INFO"}, f"created {g.name}")
        return {"FINISHED"}


class TW_OT_fit_garment(bpy.types.Operator):
    bl_idname = "trellis_workbench.fit_garment"
    bl_label = "Fit generated garment"
    bl_description = "Rest-pose masked shrinkwrap of the active garment onto the other selected body, baked to a copy"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        g = context.active_object
        bodies = [o for o in context.selected_objects if o != g and o.type == "MESH"]
        if g is None or len(bodies) != 1:
            self.report({"ERROR"}, "select the body, then the garment (active)")
            return {"CANCELLED"}
        p = _props(context)
        try:
            out = core.fit_generated_garment(context, g, bodies[0], p.clearance, p.fit_distance)
        except core.WorkbenchError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        self.report({"INFO"}, f"fitted copy {out.name}; check it, then transfer weights")
        return {"FINISHED"}


class TW_OT_transfer_weights(bpy.types.Operator):
    bl_idname = "trellis_workbench.transfer_weights"
    bl_label = "Transfer weights from body"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        g = context.active_object
        bodies = [o for o in context.selected_objects if o != g and o.type == "MESH"]
        if g is None or len(bodies) != 1:
            self.report({"ERROR"}, "select the body, then the garment (active)")
            return {"CANCELLED"}
        try:
            rep = core.transfer_weights(g, bodies[0], _props(context).transfer_distance)
        except core.WorkbenchError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        _props(context).report = json.dumps(rep)
        level = {"WARNING"} if rep["unweighted"] or rep["cross_side"] else {"INFO"}
        self.report(level, f"unweighted {rep['unweighted']}, cross-side {rep['cross_side']} of {rep['vertices']}")
        return {"FINISHED"}


class TW_OT_check_penetration(bpy.types.Operator):
    bl_idname = "trellis_workbench.check_penetration"
    bl_label = "Check penetration (poses)"
    bl_description = "Advisory: count garment vertices inside the body across inspection poses"

    def execute(self, context):
        g = context.active_object
        bodies = [o for o in context.selected_objects if o != g and o.type == "MESH"]
        if g is None or len(bodies) != 1:
            self.report({"ERROR"}, "select the body, then the garment (active)")
            return {"CANCELLED"}
        arm = core.body_armature(bodies[0])
        results = {label: core.penetration_count(g, bodies[0])["inside"] for label in core.inspection_poses(arm)}
        _props(context).report = json.dumps(results)
        self.report({"INFO"}, f"inside-body vertices per pose (advisory): {results}")
        return {"FINISHED"}


# --- UI ----------------------------------------------------------------------------------------


class TW_PT_panel(bpy.types.Panel):
    bl_label = "TRELLIS Workbench"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "TRELLIS"

    def draw(self, context):
        p = _props(context)
        col = self.layout.column()
        box = col.box()
        box.label(text="Generate", icon="IMAGE_DATA")
        box.prop(p, "reference")
        box.prop(p, "mask")
        row = box.row()
        row.prop(p, "mode")
        row.prop(p, "profile")
        box.prop(p, "backend")
        box.prop(p, "shape_only")
        box.operator(TW_OT_generate.bl_idname, icon="PLAY")
        box.operator(TW_OT_refresh.bl_idname, icon="FILE_REFRESH")
        for line in Helper.log[-5:]:
            box.label(text=line[:90])
        jobs = next((r["data"]["jobs"] for r in reversed(Helper.replies) if "jobs" in r.get("data", {})), [])
        for j in jobs[:5]:
            row = box.row()
            row.label(text=f"{j['name']} [{j['mode']}] {j['state']}")
            if j["state"] in ("submitted", "running"):
                row.operator(TW_OT_cancel.bl_idname, text="", icon="CANCEL").job_id = j["job_id"]

        box = col.box()
        box.label(text="Import", icon="IMPORT")
        bundles = next((r["data"]["bundles"] for r in reversed(Helper.replies) if "bundles" in r.get("data", {})), [])
        for b in bundles[-8:]:
            box.operator(
                TW_OT_import.bl_idname, text=f"{b['display_name']} r{b['revision']} ({b['category']})"
            ).path = b["path"]
        box.prop(p, "bundle")
        box.operator(TW_OT_import.bl_idname, text="Import bundle folder")
        box.prop(p, "height")
        box.operator(TW_OT_set_height.bl_idname)

        box = col.box()
        box.label(text="Rigid accessory", icon="CONSTRAINT_BONE")
        box.operator(TW_OT_add_landmarks.bl_idname)
        box.prop(p, "allow_scale")
        box.operator(TW_OT_align.bl_idname)
        box.prop(p, "bone")
        box.operator(TW_OT_attach.bl_idname)

        box = col.box()
        box.label(text="Garment", icon="MOD_CLOTH")
        box.prop(p, "clearance")
        box.prop(p, "thickness")
        box.operator(TW_OT_garment_from_body.bl_idname)
        box.prop(p, "fit_distance")
        box.operator(TW_OT_fit_garment.bl_idname)
        box.prop(p, "transfer_distance")
        box.operator(TW_OT_transfer_weights.bl_idname)
        box.operator(TW_OT_check_penetration.bl_idname)
        if p.report:
            box.label(text=p.report[:90])
        if context.active_object is not None:
            col.label(text=f"Status: {core.status_label(context.active_object)}")


CLASSES = (
    TW_Props,
    TW_OT_generate,
    TW_OT_refresh,
    TW_OT_cancel,
    TW_OT_import,
    TW_OT_set_height,
    TW_OT_add_landmarks,
    TW_OT_align,
    TW_OT_attach,
    TW_OT_garment_from_body,
    TW_OT_fit_garment,
    TW_OT_transfer_weights,
    TW_OT_check_penetration,
    TW_PT_panel,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.trellis_workbench = bpy.props.PointerProperty(type=TW_Props)


def unregister():
    Helper.stop()
    if bpy.app.timers.is_registered(poll_helper):
        bpy.app.timers.unregister(poll_helper)
    del bpy.types.Scene.trellis_workbench
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
