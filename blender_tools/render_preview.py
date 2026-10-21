"""Render a turntable-style preview of a bundle's GLB with Cycles (headless, CPU).

    blender --background --factory-startup --python blender_tools/render_preview.py -- BUNDLE_DIR OUT.png [size]

Uses the add-on's own verified import path, so the preview shows exactly what an artist gets.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import bpy
from mathutils import Vector

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "blender_addon"))
from trellis_workbench import core

args = sys.argv[sys.argv.index("--") + 1 :]
bundle, out = args[0], Path(args[1])
size = int(args[2]) if len(args) > 2 else 512

bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
coll = core.import_bundle(bpy.context, bundle)
objs = [o for o in coll.all_objects if o.type == "MESH"]
pts = [o.matrix_world @ Vector(c) for o in objs for c in o.bound_box]
lo = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
hi = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
center, radius = (lo + hi) / 2, max((hi - lo).length / 2, 1e-3)

cam = bpy.data.objects.new("PreviewCam", bpy.data.cameras.new("PreviewCam"))
scene.collection.objects.link(cam)
scene.camera = cam
direction = Vector((math.sin(math.radians(35)), -math.cos(math.radians(35)), 0.35)).normalized()
cam.location = center + direction * radius * 3.2
cam.rotation_euler = (center - cam.location).to_track_quat("-Z", "Y").to_euler()
cam.data.lens = 50

for name, energy, rot in (("Key", 4.0, (50, 0, 30)), ("Fill", 1.5, (60, 0, -120)), ("Rim", 2.5, (120, 0, 180))):
    light = bpy.data.objects.new(name, bpy.data.lights.new(name, "SUN"))
    light.data.energy = energy
    light.rotation_euler = tuple(math.radians(a) for a in rot)
    scene.collection.objects.link(light)
world = bpy.data.worlds.new("Preview")
world.use_nodes = True
world.node_tree.nodes["Background"].inputs["Color"].default_value = (0.18, 0.18, 0.2, 1)
scene.world = world

scene.render.engine = "CYCLES"
scene.cycles.device = "CPU"
scene.cycles.samples = 24
scene.render.resolution_x = scene.render.resolution_y = size
scene.render.film_transparent = False
scene.render.filepath = str(out)
bpy.ops.render.render(write_still=True)
print(f"preview written: {out}")
