"""Write procedural asset bundles with the real bundle code (no models, no GPU).

Usage: python make_fixtures.py OUT_DIR  -> prints a JSON map name -> bundle path
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import numpy as np
import trimesh

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from assets import textured_box_glb

from trellis_workbench import bundle as b
from trellis_workbench.mesh import validate_glb
from trellis_workbench.schema import (
    Category,
    GenerationSettings,
    Licensing,
    Manifest,
    RuntimeProvenance,
    now,
)


def sleeve_glb() -> bytes:
    """An open tube (two openings) with a flared hem: a garment-like reference mesh (glTF Y up)."""
    rings, seg = 12, 24
    verts, faces = [], []
    for r in range(rings):
        y = r / (rings - 1) * 0.5  # 0.5 m tall
        radius = 0.17 + (0.12 if r == 0 else 0.0)  # flared bottom ring = loose hem
        for s in range(seg):
            a = 2 * np.pi * s / seg
            verts.append((radius * np.cos(a), y, radius * np.sin(a)))
    for r in range(rings - 1):
        for s in range(seg):
            a, b2 = r * seg + s, r * seg + (s + 1) % seg
            c, d = a + seg, b2 + seg
            faces += [(a, c, b2), (b2, c, d)]
    m = trimesh.Trimesh(np.array(verts), np.array(faces), process=False)
    m.visual = trimesh.visual.TextureVisuals(
        uv=np.c_[np.arange(len(verts)) % seg / seg, np.arange(len(verts)) // seg / rings],
        material=trimesh.visual.material.PBRMaterial(name="cloth", baseColorFactor=[60, 90, 200, 255]),
    )
    return m.export(file_type="glb")


def thin_plate_glb() -> bytes:
    m = trimesh.creation.box(extents=(0.3, 0.2, 0.001))
    m.visual = trimesh.visual.TextureVisuals(
        uv=np.zeros((len(m.vertices), 2)), material=trimesh.visual.material.PBRMaterial(name="plate")
    )
    return m.export(file_type="glb")


def publish(
    root: Path, name: str, category: Category, glb: bytes, asset_id: str | None = None, revision: int = 1
) -> Path:
    aid = asset_id or uuid.uuid4().hex
    st = b.Staging(root, aid)
    files = [st.add_bytes("working/model.glb", glb, "working-glb")]
    try:
        report = validate_glb(st.dir / "working/model.glb", category)
    except Exception as exc:  # noqa: BLE001 - the "broken" fixture is deliberately unreadable
        report = {"result": "error", "detail": str(exc)}
    files.append(st.add_bytes("reports/mesh.json", json.dumps(report).encode(), "report"))
    m = Manifest(
        asset_id=aid,
        revision=revision,
        display_name=name,
        category=category,
        intended_use="test fixture",
        created=now(),
        job_id="fixture",
        prompt_id=str(uuid.uuid4()),
        workflow_sha256="0" * 64,
        input_hashes={"reference": "0" * 64},
        settings=GenerationSettings(
            mode=category,
            profile="standard",
            seed=0,
            resolution=1024,
            texture_size=8,
            face_budget=1000,
            diffusion_model="fixture",
            precision="none",
        ),
        runtime=RuntimeProvenance(
            backend_url="fixture",
            comfyui_version=None,
            pytorch_version=None,
            python_version=None,
            device_name=None,
            device_type=None,
            vram_total_bytes=None,
        ),
        files=files,
        validation={"result": report["result"]},
        licensing=Licensing(profile="mixed", components=[]),
    )
    return st.publish(m)


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    probe = publish(out, "probe", Category.accessory, textured_box_glb())
    aid = probe.parent.name
    paths = {
        "probe": probe,
        "probe_r2": publish(out, "probe", Category.accessory, textured_box_glb((1.0, 2.0, 0.6)), aid, 2),
        "sleeve": publish(out, "sleeve", Category.garment, sleeve_glb()),
        "plate": publish(out, "plate", Category.accessory, thin_plate_glb()),
        "broken": publish(out, "broken", Category.accessory, b"glTF-not-really"),
    }
    print(json.dumps({k: str(v) for k, v in paths.items()}))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
