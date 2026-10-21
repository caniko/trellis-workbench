"""Versioned JSON contracts shared by the CLI, bundles and the Blender add-on.

Only plain JSON crosses process boundaries; these models validate it on both sides.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 1

Sha256 = str  # lowercase hex; validated where produced


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Category(StrEnum):
    accessory = "accessory"
    garment = "garment"
    human = "human"
    animal = "animal"


class AssetStatus(StrEnum):
    """Integration readiness, assigned from checks, never from the prompt preset."""

    static = "static"  # look-development / static placement only
    retopology_needed = "retopology-needed"
    deformation_validated = "deformation-validated"  # only after an artist-run deformation check


class JobState(StrEnum):
    created = "created"
    deferred = "deferred"  # backend under memory pressure; nothing was submitted
    submitted = "submitted"
    running = "running"
    succeeded = "succeeded"  # ComfyUI execution_success, reconciled with history
    failed = "failed"
    cancelled = "cancelled"
    published = "published"  # validated bundle atomically published
    bundle_failed = "bundle_failed"
    lost = "lost"  # absent from history and queue after reconciliation (backend restart)


TERMINAL_STATES = {JobState.failed, JobState.cancelled, JobState.published, JobState.bundle_failed}


class FileRecord(Strict):
    path: str  # POSIX path relative to the bundle root
    sha256: Sha256
    size: int
    role: Literal[
        "reference", "processed-input", "mask", "source-glb", "working-glb", "texture", "preview", "report", "log"
    ]


class Coordinates(Strict):
    """glTF 2.0 conventions; Blender's importer converts to +Z up exactly once."""

    convention: Literal["gltf-2.0"] = "gltf-2.0"
    up_axis: Literal["+Y"] = "+Y"
    forward_axis: Literal["+Z"] = "+Z"
    units: Literal["meter"] = "meter"
    # TRELLIS.2 output is normalised to a unit cube: it has no trustworthy real-world size.
    scale_trusted: bool = False
    assigned_height_m: float | None = None


class RuntimeProvenance(Strict):
    backend_url: str
    comfyui_version: str | None
    pytorch_version: str | None
    python_version: str | None
    device_name: str | None
    device_type: str | None
    vram_total_bytes: int | None


class ModelRef(Strict):
    filename: str
    folder: str
    license: str


class GenerationSettings(Strict):
    mode: Category
    profile: str
    seed: int
    resolution: int
    texture_size: int
    face_budget: int
    diffusion_model: str
    precision: str
    attention: str = "comfyui-default"
    offload: str = "comfyui-smart-memory"
    remove_background: bool = True
    overrides: dict[str, int | float | str | bool] = Field(default_factory=dict)


class Event(Strict):
    at: datetime
    kind: str
    detail: str


class Licensing(Strict):
    profile: Literal["mixed", "strict-foss"]
    components: list[ModelRef]
    reference_rights: str = "unspecified"
    note: str = (
        "Software and model licenses are tracked separately from the rights to the reference image. "
        "No ownership, originality or permission is asserted for generated output."
    )


class Manifest(Strict):
    schema_version: Literal[1] = SCHEMA_VERSION
    asset_id: str
    revision: int
    parent_revision: int | None = None
    display_name: str
    category: Category
    intended_use: str
    status: AssetStatus = AssetStatus.static
    created: datetime
    job_id: str
    prompt_id: str
    workflow_sha256: Sha256
    input_hashes: dict[str, Sha256]
    settings: GenerationSettings
    runtime: RuntimeProvenance
    coordinates: Coordinates = Coordinates()
    files: list[FileRecord]
    validation: dict  # mesh report summary (see mesh.py), kept schemaless but JSON-only
    licensing: Licensing
    events: list[Event] = Field(default_factory=list)


def now() -> datetime:
    return datetime.now(UTC)


# --- Blender add-on <-> helper process messages (JSON lines) ---------------------------------


class AddonRequest(Strict):
    schema_version: Literal[1] = SCHEMA_VERSION
    id: str
    op: Literal["ping", "doctor", "submit", "status", "cancel", "list_bundles", "shutdown"]
    args: dict = Field(default_factory=dict)


class AddonReply(Strict):
    schema_version: Literal[1] = SCHEMA_VERSION
    id: str | None  # None for unsolicited progress events
    ok: bool
    kind: Literal["result", "progress", "error"]
    data: dict = Field(default_factory=dict)
