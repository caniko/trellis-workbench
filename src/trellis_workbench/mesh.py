"""Read-only mesh validation for generated GLBs, classified by asset purpose.

Findings are facts plus a severity that depends on the category: an open boundary is
expected on a garment (cuffs, hems, necklines) but suspicious on a solid accessory.
Nothing here modifies geometry.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import numpy as np
import trimesh
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

from .schema import Category

# category -> severity of an open boundary / multiple components
BOUNDARY_SEVERITY = {
    Category.accessory: "warning",
    Category.garment: "info",
    Category.human: "info",
    Category.animal: "info",
}


def _finding(code: str, severity: str, count: int, detail: str) -> dict:
    return {"code": code, "severity": severity, "count": int(count), "detail": detail}


def _boundary_loops(boundary_edges: np.ndarray) -> int:
    """Number of closed boundary loops (connected components of the boundary edge graph)."""
    if len(boundary_edges) == 0:
        return 0
    parent: dict[int, int] = {}

    def find(a: int) -> int:
        while parent.setdefault(a, a) != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for a, b in boundary_edges:
        ra, rb = find(int(a)), find(int(b))
        if ra != rb:
            parent[ra] = rb
    return len({find(int(v)) for v in boundary_edges.ravel()})


def validate_geometry(
    vertices: np.ndarray, faces: np.ndarray, uv: np.ndarray | None, category: Category, name: str
) -> list[dict]:
    out: list[dict] = []
    nv = len(vertices)
    if faces.size and (faces.min() < 0 or faces.max() >= nv):
        bad = int(((faces < 0) | (faces >= nv)).any(axis=1).sum())
        return [_finding("invalid-indices", "error", bad, f"{name}: faces reference missing vertices")]
    nonfinite = int((~np.isfinite(vertices)).any(axis=1).sum())
    if nonfinite:
        out.append(_finding("non-finite-vertices", "error", nonfinite, f"{name}: NaN/inf vertex positions"))
        return out

    tri = vertices[faces]
    area2 = np.linalg.norm(np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]), axis=1)
    scale = float(np.ptp(vertices, axis=0).max()) or 1.0
    degenerate = int((area2 <= (scale * 1e-7) ** 2).sum())
    if degenerate:
        out.append(_finding("degenerate-faces", "warning", degenerate, f"{name}: zero-area triangles"))

    dup = len(faces) - len(np.unique(np.sort(faces, axis=1), axis=0))
    if dup:
        out.append(_finding("duplicate-faces", "warning", dup, f"{name}: faces sharing all three vertices"))

    edges = np.sort(faces[:, [0, 1, 1, 2, 2, 0]].reshape(-1, 2), axis=1)
    uniq, counts = np.unique(edges, axis=0, return_counts=True)
    boundary = uniq[counts == 1]
    nonmanifold = int((counts > 2).sum())
    if len(boundary):
        loops = _boundary_loops(boundary)
        out.append(
            _finding(
                "open-boundary",
                BOUNDARY_SEVERITY[category],
                loops,
                f"{name}: {loops} boundary loop(s), {len(boundary)} boundary edges"
                + (" (expected for garments: cuffs, hems, openings)" if category == Category.garment else ""),
            )
        )
    if nonmanifold:
        out.append(_finding("non-manifold-edges", "warning", nonmanifold, f"{name}: edges shared by >2 faces"))

    used = np.unique(faces)
    graph = coo_matrix((np.ones(len(edges)), (edges[:, 0], edges[:, 1])), shape=(nv, nv))
    _n, labels = connected_components(graph, directed=False)
    components = len(np.unique(labels[used])) if len(used) else 0
    if components > 1:
        out.append(
            _finding(
                "disconnected-components",
                "info",
                components,
                f"{name}: {components} separate parts (eyes, buttons, claws may be intentional; nothing removed)",
            )
        )

    if uv is None:
        out.append(_finding("no-uv", "warning", 1, f"{name}: no TEXCOORD_0; textures cannot be mapped"))
    else:
        bad_uv = int((~np.isfinite(uv)).any(axis=1).sum())
        if bad_uv:
            out.append(_finding("non-finite-uv", "error", bad_uv, f"{name}: NaN/inf texture coordinates"))
        outside = int(((uv < -1e-4) | (uv > 1 + 1e-4)).any(axis=1).sum())
        if outside:
            out.append(_finding("uv-out-of-range", "info", outside, f"{name}: UVs outside [0,1] (tiling or error)"))
        t = uv[faces]
        e1, e2 = t[:, 1] - t[:, 0], t[:, 2] - t[:, 0]
        signed = e1[:, 0] * e2[:, 1] - e1[:, 1] * e2[:, 0]
        zero = int((np.abs(signed) < 1e-12).sum())
        flipped = int(min((signed < 0).sum(), (signed > 0).sum()))
        if zero:
            out.append(_finding("uv-degenerate", "warning", zero, f"{name}: zero-area UV triangles"))
        if flipped:
            out.append(_finding("uv-flipped", "info", flipped, f"{name}: UV triangles with minority winding"))
    return out


def validate_glb(path: Path, category: Category) -> dict:
    scene = trimesh.load(path, force="scene", process=False)
    findings: list[dict] = []
    stats = Counter()
    materials = set()
    for name, geom in scene.geometry.items():
        if not isinstance(geom, trimesh.Trimesh):
            findings.append(_finding("non-mesh-geometry", "info", 1, f"{name}: {type(geom).__name__} ignored"))
            continue
        v = np.asarray(geom.vertices, dtype=np.float64)
        f = np.asarray(geom.faces, dtype=np.int64)
        uv = getattr(geom.visual, "uv", None)
        uv = None if uv is None else np.asarray(uv, dtype=np.float64)
        stats["vertices"] += len(v)
        stats["faces"] += len(f)
        stats["meshes"] += 1
        mat = getattr(geom.visual, "material", None)
        if mat is not None:
            materials.add(getattr(mat, "name", None) or repr(type(mat).__name__))
        findings.extend(validate_geometry(v, f, uv, category, name))
    if stats["meshes"] == 0:
        findings.append(_finding("empty", "error", 1, "no triangle meshes in GLB"))
    bounds = scene.bounds.tolist() if stats["meshes"] else None
    findings.append(
        _finding(
            "intersections-not-certified",
            "info",
            0,
            "self-/inter-part intersection is not checked exactly; inspect visually",
        )
    )
    worst = (
        "error"
        if any(x["severity"] == "error" for x in findings)
        else "warning"
        if any(x["severity"] == "warning" for x in findings)
        else "ok"
    )
    return {
        "schema_version": 1,
        "category": category.value,
        "result": worst,
        "stats": dict(stats) | {"materials": len(materials), "bounds": bounds},
        "findings": findings,
    }
