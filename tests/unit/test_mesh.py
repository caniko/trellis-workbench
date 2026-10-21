"""Category-aware mesh findings on procedural geometry."""

from __future__ import annotations

import numpy as np
import trimesh

from trellis_workbench.mesh import validate_geometry, validate_glb
from trellis_workbench.schema import Category


def codes(findings):
    return {f["code"]: f for f in findings}


def open_tube():
    m = trimesh.creation.cylinder(radius=0.5, height=1.0, sections=16)
    keep = np.abs(m.face_normals[:, 2]) < 0.5  # drop caps: a sleeve with two openings
    return m.vertices, m.faces[keep]


def test_open_boundary_severity_depends_on_category():
    v, f = open_tube()
    garment = codes(validate_geometry(v, f, None, Category.garment, "sleeve"))
    accessory = codes(validate_geometry(v, f, None, Category.accessory, "ring"))
    assert garment["open-boundary"]["severity"] == "info" and garment["open-boundary"]["count"] == 2
    assert accessory["open-boundary"]["severity"] == "warning"


def test_errors_and_defects():
    v = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [np.nan, 0, 0]], float)
    assert codes(validate_geometry(v, np.array([[0, 1, 2]]), None, Category.human, "x")).get("non-finite-vertices")
    v = v[:3]
    assert "invalid-indices" in codes(validate_geometry(v, np.array([[0, 1, 5]]), None, Category.human, "x"))
    f = np.array([[0, 1, 2], [2, 1, 0], [0, 0, 1]])
    c = codes(validate_geometry(v, f, None, Category.human, "x"))
    assert c["duplicate-faces"]["count"] == 1 and c["degenerate-faces"]["count"] == 1 and "no-uv" in c


def test_separate_parts_are_reported_not_removed():
    a = trimesh.creation.box()
    b = trimesh.creation.icosphere(radius=0.1)
    b.apply_translation([2, 0, 0])
    m = trimesh.util.concatenate([a, b])
    c = codes(validate_geometry(m.vertices, m.faces, None, Category.animal, "eye+body"))
    assert c["disconnected-components"]["count"] == 2 and c["disconnected-components"]["severity"] == "info"


def test_glb_report(tmp_path):
    from assets import textured_box_glb

    p = tmp_path / "box.glb"
    p.write_bytes(textured_box_glb())
    r = validate_glb(p, Category.accessory)
    assert r["result"] in {"ok", "warning"} and r["stats"]["faces"] == 12
    assert "no-uv" not in {f["code"] for f in r["findings"]}


def test_uv_seam_splits_are_not_reported_as_openings():
    """A closed box with per-face (seam-split) vertices is still one closed part."""
    box = trimesh.creation.box()
    box.unmerge_vertices()  # every face gets its own vertices, as in a UV-seamed glTF
    c = codes(validate_geometry(box.vertices, box.faces, np.zeros((len(box.vertices), 2)), Category.accessory, "box"))
    assert "open-boundary" not in c and "disconnected-components" not in c and "non-manifold-edges" not in c
