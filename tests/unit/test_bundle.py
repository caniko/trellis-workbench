"""Bundle publication: atomic, immutable, checksummed, path-safe."""

from __future__ import annotations

import uuid

import pytest

from trellis_workbench import bundle as b
from trellis_workbench.comfy import ComfyError, OutputRef
from trellis_workbench.schema import Category, GenerationSettings, Licensing, Manifest, RuntimeProvenance, now


def manifest(asset_id, files, revision=1):
    return Manifest(
        asset_id=asset_id,
        revision=revision,
        display_name="probe",
        category=Category.accessory,
        intended_use="test",
        created=now(),
        job_id="j",
        prompt_id=str(uuid.uuid4()),
        workflow_sha256="0" * 64,
        input_hashes={"reference": "0" * 64},
        settings=GenerationSettings(
            mode=Category.accessory,
            profile="standard",
            seed=1,
            resolution=1024,
            texture_size=2048,
            face_budget=1000,
            diffusion_model="m",
            precision="bf16",
        ),
        runtime=RuntimeProvenance(
            backend_url="http://x",
            comfyui_version=None,
            pytorch_version=None,
            python_version=None,
            device_name=None,
            device_type=None,
            vram_total_bytes=None,
        ),
        files=files,
        validation={},
        licensing=Licensing(profile="mixed", components=[]),
    )


def staged(root, asset_id):
    st = b.Staging(root, asset_id)
    files = [
        st.add_bytes("working/model.glb", b"glb", "working-glb"),
        st.add_bytes("reports/mesh.json", b"{}", "report"),
    ]
    return st, files


def test_publish_is_atomic_and_never_overwrites(tmp_path):
    aid = uuid.uuid4().hex
    st, files = staged(tmp_path, aid)
    assert b.list_bundles(tmp_path) == []  # staging is invisible
    out = st.publish(manifest(aid, files))
    assert out == tmp_path / aid / "r0001" and b.verify(out).revision == 1
    st2, files2 = staged(tmp_path, aid)
    with pytest.raises(b.BundleError, match="already exists"):
        st2.publish(manifest(aid, files2))
    assert b.verify(out).files == files  # first revision untouched
    (st2.dir / "manifest.json").unlink()
    out2 = st2.publish(manifest(aid, files2, revision=2))
    assert b.revisions(tmp_path, aid) == [1, 2] and out2.name == "r0002"


def test_failed_staging_leaves_no_revision(tmp_path):
    aid = uuid.uuid4().hex
    st, files = staged(tmp_path, aid)
    with pytest.raises(b.BundleError, match="differs"):
        st.publish(manifest(aid, files[:1]))
    st.discard()
    assert b.revisions(tmp_path, aid) == []


def test_verify_detects_tampering_and_extras(tmp_path):
    aid = uuid.uuid4().hex
    st, files = staged(tmp_path, aid)
    out = st.publish(manifest(aid, files))
    (out / "working/model.glb").chmod(0o644)
    (out / "working/model.glb").write_bytes(b"GLB")
    with pytest.raises(b.BundleError, match="checksum"):
        b.verify(out)
    (out / "working/model.glb").write_bytes(b"glb")
    (out / "evil.py").write_text("import os")
    with pytest.raises(b.BundleError, match="unlisted"):
        b.verify(out)


@pytest.mark.parametrize("rel", ["../x", "/abs", "a/../../b", "", "a\\b", "x/\0y"])
def test_unsafe_bundle_paths_rejected(tmp_path, rel):
    st = b.Staging(tmp_path, uuid.uuid4().hex)
    with pytest.raises(b.BundleError):
        st.add_bytes(rel, b"x", "log")


def test_asset_root_allowlist(tmp_path):
    assert b.check_root(tmp_path / "a", [tmp_path]) == (tmp_path / "a").resolve()
    with pytest.raises(b.BundleError):
        b.check_root(tmp_path / ".." / "elsewhere", [tmp_path])
    with pytest.raises(b.BundleError):
        b.Staging(tmp_path, "../../etc")


@pytest.mark.parametrize(
    "ref",
    [
        OutputRef("../secret", "", "output"),
        OutputRef("a/b.glb", "", "output"),
        OutputRef("x.glb", "../..", "output"),
        OutputRef("x.glb", "/etc", "output"),
        OutputRef("x.glb", "", "input"),
        OutputRef("..", "", "temp"),
    ],
)
def test_artifact_references_rejected(ref):
    with pytest.raises(ComfyError):
        ref.checked()
