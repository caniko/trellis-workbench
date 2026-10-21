"""API-format workflows with named bindings, validated against the backend's /object_info.

Workflows are addressed by node title (`_meta.title`) and input *name*, never by positional
widget index, so a schema change surfaces as a validation error instead of a silent
mis-assignment.
"""

from __future__ import annotations

import copy
import hashlib
import json
import tomllib
from dataclasses import dataclass
from importlib import resources
from pathlib import Path


class WorkflowError(ValueError):
    pass


@dataclass(frozen=True)
class Binding:
    node_title: str
    input: str
    slot: int | None = None  # set for link bindings: value is the source node's title


# Inputs the server validates itself (VALIDATE_INPUTS) instead of against the listed choices:
# uploaded images may live in a per-job input subfolder.
COMBO_EXEMPT = {("LoadImage", "image"), ("LoadImageMask", "image")}


@dataclass(frozen=True)
class Preset:
    name: str
    api: dict
    bindings: dict[str, list[Binding]]
    profiles: dict[str, dict]
    modes: dict[str, dict]
    outputs: dict[str, list[str]]  # bundle role -> node titles producing it
    stages: dict[str, list[str]]  # stage -> output node titles to execute


def _workflow_dir() -> Path:
    return Path(str(resources.files("trellis_workbench") / "workflows"))


def load_preset(name: str = "trellis2", directory: Path | None = None) -> Preset:
    d = directory or _workflow_dir()
    spec = tomllib.loads((d / f"{name}.presets.toml").read_text())
    api = json.loads((d / spec["workflow"]["api"]).read_text())
    bindings = {}
    for role, raw in spec["bindings"].items():
        items = raw if isinstance(raw, list) else [raw]
        bindings[role] = [Binding(i["node"], i["input"], i.get("slot")) for i in items]
    outputs = {role: v if isinstance(v, list) else [v] for role, v in spec["outputs"].items()}
    return Preset(name, api, bindings, spec["profiles"], spec["modes"], outputs, spec["stages"])


def node_ids_by_title(api: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for nid, node in api.items():
        title = node.get("_meta", {}).get("title")
        if title:
            if title in out:
                raise WorkflowError(f"duplicate node title {title!r}")
            out[title] = nid
    return out


def bind(preset: Preset, values: dict[str, object]) -> dict:
    """Copy the API workflow and set every bound input. Unknown roles are errors."""
    api = copy.deepcopy(preset.api)
    ids = node_ids_by_title(api)
    for role, value in values.items():
        if role not in preset.bindings:
            raise WorkflowError(f"unknown binding role {role!r}")
        for b in preset.bindings[role]:
            if b.node_title not in ids:
                raise WorkflowError(f"binding {role!r} targets missing node {b.node_title!r}")
            inputs = api[ids[b.node_title]]["inputs"]
            if b.input not in inputs:
                raise WorkflowError(f"binding {role!r}: node {b.node_title!r} has no input {b.input!r}")
            is_link = isinstance(inputs[b.input], list)
            if (b.slot is not None) != is_link:
                raise WorkflowError(f"binding {role!r}: {b.node_title}.{b.input} link/value kind mismatch")
            if b.slot is None:
                inputs[b.input] = value
            else:
                if value not in ids:
                    raise WorkflowError(f"binding {role!r}: link source {value!r} is not a node title")
                inputs[b.input] = [ids[value], b.slot]
    return api


def prune(api: dict, output_titles: list[str]) -> dict:
    """Keep only the nodes the selected output nodes depend on."""
    ids = node_ids_by_title(api)
    missing = [t for t in output_titles if t not in ids]
    if missing:
        raise WorkflowError(f"unknown output nodes {missing}")
    keep: set[str] = set()
    stack = [ids[t] for t in output_titles]
    while stack:
        nid = stack.pop()
        if nid in keep:
            continue
        keep.add(nid)
        for v in api[nid]["inputs"].values():
            if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str):
                stack.append(v[0])
    return {nid: node for nid, node in api.items() if nid in keep}


def _spec(object_info: dict, class_type: str) -> tuple[dict, dict]:
    info = object_info.get(class_type)
    if info is None:
        raise KeyError(class_type)
    inp = info.get("input", {})
    return inp.get("required", {}) or {}, inp.get("optional", {}) or {}


def _options(spec) -> list | None:
    """COMBO choices in either the legacy ([choices], {...}) or the ("COMBO", {"options": ...}) form."""
    if isinstance(spec, list) and spec:
        if isinstance(spec[0], list):
            return spec[0]
        if spec[0] == "COMBO" and len(spec) > 1 and isinstance(spec[1], dict):
            return spec[1].get("options")
    return None


def validate(api: dict, object_info: dict) -> list[str]:
    """Every problem that would make this workflow stale against the backend schema."""
    problems = []
    for nid, node in api.items():
        ct = node.get("class_type")
        where = f"node {nid} ({node.get('_meta', {}).get('title', ct)})"
        try:
            required, optional = _spec(object_info, ct)
        except KeyError:
            problems.append(f"{where}: node type {ct!r} not provided by the backend")
            continue
        known = required | optional
        inputs = node.get("inputs", {})
        for name in inputs:
            # Dynamic-combo sub-inputs are flattened as "<input>.<option input>".
            if name not in known and name.split(".", 1)[0] not in known:
                problems.append(f"{where}: input {name!r} is not in the backend schema")
        for name in required:
            if name not in inputs:
                problems.append(f"{where}: required input {name!r} missing")
        for name, value in inputs.items():
            if name not in known:
                continue
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str):
                if value[0] not in api:
                    problems.append(f"{where}: input {name!r} links to missing node {value[0]}")
                continue
            spec = known[name]
            opts = _options(spec)
            if (ct, name) in COMBO_EXEMPT:
                opts = None
            if opts is not None and value not in opts:
                problems.append(f"{where}: {name}={value!r} not among backend choices ({len(opts)} available)")
            meta = spec[1] if isinstance(spec, list) and len(spec) > 1 and isinstance(spec[1], dict) else {}
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if "min" in meta and value < meta["min"]:
                    problems.append(f"{where}: {name}={value} below minimum {meta['min']}")
                if "max" in meta and value > meta["max"]:
                    problems.append(f"{where}: {name}={value} above maximum {meta['max']}")
    return problems


def sha256_json(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
