"""The exact weights a job loads: pinned revisions and checksums.

`model_lock.json` is the runtime copy of the model rows in `docs/inventory.json`
(`tests/unit/test_model_lock.py` fails when they drift). A cache key that names a profile but not
the checkpoint behind it cannot tell a model update from a cache hit.

Keys are bare filenames: that is how workflow inputs name models and what the manifest's
`licensing.components` uses.

A checkpoint-shaped name that is not in the lock is rejected: silently omitting it would
fingerprint a job without the model it loads. The backend exposes filenames only (`/models`), not
file identities, so matching the deployment's hashes relies on it pinning the same revisions as
this lock (see docs/architecture.md).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from importlib import resources
from pathlib import Path

# Names with these suffixes are checkpoints. A workflow's other strings (mask sources, node
# titles, uploaded image paths) never have one.
MODEL_SUFFIXES = (".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf")


def _load() -> dict[str, dict]:
    raw = json.loads(Path(str(resources.files("trellis_workbench") / "model_lock.json")).read_text())
    return {m["file"].rsplit("/", 1)[-1]: {"revision": m["revision"], "sha256": m["sha256"]} for m in raw["models"]}


LOCK: dict[str, dict] = _load()


def used(api: dict, values: Iterable[object] = ()) -> dict[str, dict]:
    """Revisions of every locked model named by this API graph or by the resolved parameter values.

    Raises ValueError for a checkpoint name the lock does not pin, so a new or renamed model can
    never enter the fingerprint (or the manifest's licensing) unchecked.
    """
    named = {v for node in api.values() for v in node["inputs"].values() if isinstance(v, str)}
    named |= {v for v in values if isinstance(v, str)}
    models = {v for v in named if v.endswith(MODEL_SUFFIXES)}
    unknown = sorted(models - set(LOCK))
    if unknown:
        raise ValueError(f"model {unknown[0]!r} is not pinned in model_lock.json; pin its revision and sha256")
    return {name: dict(LOCK[name]) for name in sorted(models)}
