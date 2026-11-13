"""The runtime model lock: complete for every licensed model and equal to the inventory."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trellis_workbench import model_lock
from trellis_workbench.jobs import MODEL_LICENSES


def test_every_licensed_model_is_pinned():
    assert set(MODEL_LICENSES) <= set(model_lock.LOCK)
    for name, entry in model_lock.LOCK.items():
        assert len(entry["revision"]) == 40, name
        assert len(entry["sha256"]) == 64, name


def test_lock_matches_the_inventory():
    inventory = Path(__file__).resolve().parents[2] / "docs" / "inventory.json"
    if not inventory.is_file():
        pytest.skip("docs/inventory.json is not part of the installed source tree")
    rows = {m["file"].rsplit("/", 1)[-1]: m for m in json.loads(inventory.read_text())["models"]}
    assert {n: (e["revision"], e["sha256"]) for n, e in model_lock.LOCK.items()} == {
        n: (m["revision"], m["sha256"]) for n, m in rows.items()
    }


def test_used_pins_the_models_a_graph_and_its_values_name():
    api = {"1": {"class_type": "UNETLoader", "inputs": {"unet_name": "trellis_2_bf16.safetensors"}}}
    assert model_lock.used(api, ["TW Remesh", "trellis_2_int8_convrot.safetensors"]) == {
        n: model_lock.LOCK[n] for n in ("trellis_2_bf16.safetensors", "trellis_2_int8_convrot.safetensors")
    }


def test_unknown_model_name_is_rejected():
    api = {"1": {"class_type": "UNETLoader", "inputs": {"unet_name": "trellis_9_bf16.safetensors"}}}
    with pytest.raises(ValueError, match=r"trellis_9_bf16.*not pinned in model_lock.json"):
        model_lock.used(api)
    with pytest.raises(ValueError, match="trellis_9_bf16"):
        model_lock.used({}, ["trellis_9_bf16.safetensors"])
