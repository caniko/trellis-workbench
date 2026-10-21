"""Named bindings, pruning and validation against node schemas."""

from __future__ import annotations

import copy

import pytest
from conftest import schema_for

from trellis_workbench import workflow


@pytest.fixture
def preset():
    return workflow.load_preset()


def titles(api):
    return {n["_meta"]["title"] for n in api.values()}


def test_shipped_combinations_are_valid(preset):
    info = schema_for(preset.api)
    for stage in preset.stages:
        for mode, m in preset.modes.items():
            for prof, p in preset.profiles.items():
                values = {k: v for k, v in (p | m).items() if k in preset.bindings}
                api = workflow.prune(workflow.bind(preset, values), preset.stages[stage])
                assert workflow.validate(api, info) == [], (stage, mode, prof)


def test_shape_stage_prunes_texturing(preset):
    api = workflow.prune(workflow.bind(preset, {"mesh_source": "TW Remesh"}), preset.stages["shape"])
    t = titles(api)
    assert "TW Save Source" in t and "TW Texture Sampler" not in t and "TW Bake Material" not in t


def test_garment_mode_skips_volumetric_remesh(preset):
    api = workflow.prune(
        workflow.bind(preset, {"mesh_source": preset.modes["garment"]["mesh_source"]}), preset.stages["full"]
    )
    assert "TW Remesh" not in titles(api)
    accessory = workflow.prune(workflow.bind(preset, {"mesh_source": "TW Remesh"}), preset.stages["full"])
    assert "TW Remesh" in titles(accessory)


def test_binding_is_by_name(preset):
    api = workflow.bind(preset, {"seed": 99, "face_budget": 1234})
    ids = workflow.node_ids_by_title(api)
    assert api[ids["TW Shape Sampler"]]["inputs"]["seed"] == 99
    assert api[ids["TW Decimate"]]["inputs"]["target_face_count"] == 1234
    with pytest.raises(workflow.WorkflowError):
        workflow.bind(preset, {"no_such_role": 1})
    with pytest.raises(workflow.WorkflowError):
        workflow.bind(preset, {"mask_source": "TW Missing"})


def test_stale_schema_is_reported(preset):
    info = schema_for(preset.api)
    api = workflow.bind(preset, {})
    stale = copy.deepcopy(info)
    del stale["UnwrapMesh"]["input"]["required"]["weld_distance"]
    assert any("weld_distance" in p for p in workflow.validate(api, stale))
    stale = copy.deepcopy(info)
    stale["KSampler"]["input"]["required"]["new_required"] = ["INT", {}]
    assert any("new_required" in p for p in workflow.validate(api, stale))
    stale = copy.deepcopy(info)
    del stale["Trellis2TextureStage"]
    assert any("Trellis2TextureStage" in p for p in workflow.validate(api, stale))


def test_unprovisioned_model_is_reported(preset):
    info = schema_for(preset.api)
    info["UNETLoader"]["input"]["required"]["unet_name"] = [["something_else.safetensors"], {}]
    api = workflow.bind(preset, {"diffusion_model": "trellis_2_bf16.safetensors"})
    assert any("unet_name" in p for p in workflow.validate(api, info))


def test_new_style_combo_and_bounds(preset):
    info = schema_for(preset.api)
    info["UnwrapMesh"]["input"]["required"]["segmenter"] = ["COMBO", {"options": ["adaptive"]}]
    info["MeshSmoothNormals"]["input"]["required"]["crease_angle"] = ["FLOAT", {"min": 0, "max": 90}]
    problems = workflow.validate(workflow.bind(preset, {}), info)
    assert any("segmenter" in p for p in problems) and any("crease_angle" in p for p in problems)
