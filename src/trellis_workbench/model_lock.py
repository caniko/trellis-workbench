"""The exact weights a job loads: pinned revisions and checksums.

`model_lock.json` is the runtime copy of the model rows in `docs/inventory.json`
(`tests/unit/test_model_lock.py` fails when they drift). A cache key that names a profile but not
the checkpoint behind it cannot tell a model update from a cache hit.

Keys are bare filenames: that is how workflow inputs name models and what the manifest's
`licensing.components` uses.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from importlib import resources
from pathlib import Path


def _load() -> dict[str, dict]:
    raw = json.loads(Path(str(resources.files("trellis_workbench") / "model_lock.json")).read_text())
    return {m["file"].rsplit("/", 1)[-1]: {"revision": m["revision"], "sha256": m["sha256"]} for m in raw["models"]}


LOCK: dict[str, dict] = _load()


def used(api: dict, values: Iterable[object] = ()) -> dict[str, dict]:
    """Revisions of every locked model named by this API graph or by the resolved parameter values."""
    names = {v for node in api.values() for v in node["inputs"].values() if isinstance(v, str) and v in LOCK}
    names |= {v for v in values if isinstance(v, str) and v in LOCK}
    return {name: dict(LOCK[name]) for name in sorted(names)}
