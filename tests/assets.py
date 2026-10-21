"""Procedural test assets: GLB and PNG bytes with known contents (no downloads)."""

from __future__ import annotations

import io

import numpy as np
import trimesh
from PIL import Image


def textured_box_glb(size=(1.0, 2.0, 0.5)) -> bytes:
    """A box with UVs and a full glTF PBR material whose channels carry distinctive values."""
    mesh = trimesh.creation.box(extents=size)
    mesh.unmerge_vertices()
    uv = np.c_[(mesh.vertices[:, 0] / size[0]) + 0.5, (mesh.vertices[:, 1] / size[1]) + 0.5]
    base = Image.new("RGBA", (8, 8), (200, 40, 20, 255))
    orm = Image.new("RGB", (8, 8), (255, 64, 191))  # R=AO, G=roughness 0.25, B=metallic 0.75
    normal = Image.new("RGB", (8, 8), (128, 128, 255))
    mat = trimesh.visual.material.PBRMaterial(
        name="probe",
        baseColorTexture=base,
        metallicRoughnessTexture=orm,
        normalTexture=normal,
        metallicFactor=1.0,
        roughnessFactor=1.0,
        doubleSided=True,
        alphaMode="MASK",
        alphaCutoff=0.5,
    )
    mesh.visual = trimesh.visual.TextureVisuals(uv=uv, material=mat)
    return mesh.export(file_type="glb")


def png_bytes(color=(255, 0, 0, 255), size=(16, 16), mode="RGBA") -> bytes:
    buf = io.BytesIO()
    Image.new(mode, size, color[: len(mode)]).save(buf, format="PNG")
    return buf.getvalue()
