"""Job lifecycle: preflight, submit, observe, reconcile, cancel, and publish a bundle.

Execution success (ComfyUI reports execution_success and history agrees) is distinct from
bundle success (artifacts fetched, validated and atomically published). Only a published
bundle is offered for import.
"""

from __future__ import annotations

import json
import random
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import bundle as bundlelib
from . import mesh, workflow
from .comfy import ComfyClient, ComfyError, PromptRejected, history_outcome, is_oom, output_refs
from .config import Config
from .journal import Job, Journal
from .schema import (
    Category,
    Event,
    GenerationSettings,
    JobState,
    Licensing,
    Manifest,
    ModelRef,
    RuntimeProvenance,
    now,
)

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
MAX_IMAGE_BYTES = 64 * 2**20
GIB = 2**30

# Licenses of the model files each workflow loads (see docs/licenses.md).
MODEL_LICENSES = {
    "dino_v3_vit_l.safetensors": ("clip_vision", "LicenseRef-DINOv3"),
    "trellis_2_bf16.safetensors": ("diffusion_models", "MIT"),
    "trellis_2_int8_convrot.safetensors": ("diffusion_models", "MIT"),
    "trellis_2_shape_vae_bf16.safetensors": ("vae", "MIT"),
    "trellis_2_texture_vae_bf16.safetensors": ("vae", "MIT"),
    "birefnet.safetensors": ("background_removal", "MIT"),
}


@dataclass
class Request:
    image: Path
    mode: Category
    profile: str = "standard"
    seed: int | None = None
    mask: Path | None = None
    display_name: str | None = None
    asset_id: str | None = None  # new revision of an existing asset
    parent_revision: int | None = None
    reference_rights: str = "unspecified"
    overrides: dict = field(default_factory=dict)
    allow_oom_fallback: bool = True
    stage: str = "full"  # "shape" = geometry only
    preserve_alpha: bool = True  # an RGBA reference's own alpha is used instead of background removal


class JobFailed(RuntimeError):
    pass


def _sha(path: Path) -> str:
    return bundlelib.sha256_file(path)


def has_alpha(path: Path) -> bool:
    """True when the image carries a non-trivial alpha channel (intentional transparency)."""
    from PIL import Image

    with Image.open(path) as im:
        if "A" not in im.getbands() and "transparency" not in im.info:
            return False
        alpha = im.convert("RGBA").getchannel("A")
        lo, _hi = alpha.getextrema()
        return lo < 255


def _check_image(path: Path) -> Path:
    p = path.expanduser().resolve()
    if not p.is_file() or p.suffix.lower() not in IMAGE_SUFFIXES:
        raise JobFailed(f"reference must be an existing PNG/JPEG/WebP file: {path}")
    if p.stat().st_size > MAX_IMAGE_BYTES:
        raise JobFailed(f"reference image larger than {MAX_IMAGE_BYTES} bytes: {path}")
    return p


def runtime_of(url: str, stats: dict) -> RuntimeProvenance:
    sysinfo = stats.get("system", {})
    dev = (stats.get("devices") or [{}])[0]
    return RuntimeProvenance(
        backend_url=url,
        comfyui_version=sysinfo.get("comfyui_version"),
        pytorch_version=sysinfo.get("pytorch_version"),
        python_version=sysinfo.get("python_version"),
        device_name=dev.get("name"),
        device_type=dev.get("type"),
        vram_total_bytes=dev.get("vram_total"),
    )


def accelerator_or_fail(stats: dict) -> dict:
    """Refuse to run generation on a CPU-only backend; never report CPU work as GPU work."""
    dev = (stats.get("devices") or [{}])[0]
    if dev.get("type") not in {"cuda", "xpu"}:  # ROCm devices report the torch.cuda type
        raise JobFailed(f"backend has no GPU device (reported {dev.get('type')!r}); refusing to generate")
    return dev


class Runner:
    def __init__(self, cfg: Config, backend: str | None, progress: Callable[[dict], None] = lambda _e: None):
        self.cfg = cfg
        self.url = cfg.backend_url(backend)
        self.client = ComfyClient(self.url)
        self.journal = Journal(cfg.journal)
        self.root = bundlelib.check_root(cfg.asset_root, cfg.roots())
        self.progress = progress

    # --- submission -----------------------------------------------------------------------

    def run(self, req: Request) -> Job:
        preset = workflow.load_preset()
        if req.mode.value not in preset.modes:
            raise JobFailed(f"unknown mode {req.mode}")
        job = self.submit(req, preset)
        return self.follow(job.job_id)

    def submit(self, req: Request, preset: workflow.Preset) -> Job:
        image = _check_image(req.image)
        mask = _check_image(req.mask) if req.mask else None
        mode = preset.modes[req.mode.value]
        profile_name = req.profile
        if profile_name not in preset.profiles:
            raise JobFailed(f"unknown profile {profile_name!r}; available: {', '.join(preset.profiles)}")
        profile = preset.profiles[profile_name]
        if self.cfg.license_profile != "mixed":
            raise JobFailed(
                "TRELLIS.2 conditioning needs the DINOv3 encoder (DINOv3 License); the strict-FOSS "
                'profile cannot generate. Set license_profile = "mixed" after accepting its terms.'
            )

        stats = self.client.system_stats()
        dev = accelerator_or_fail(stats)
        need = int(profile["min_free_vram_gib"] * GIB)
        if dev.get("vram_free", 0) < need:
            raise JobFailed(
                f"deferred: backend has {dev.get('vram_free', 0) / GIB:.1f} GiB free VRAM, profile "
                f"{profile_name!r} needs {need / GIB:.1f} GiB. Other applications are never evicted; "
                "retry later or use --profile low-memory."
            )

        seed = req.seed if req.seed is not None else random.SystemRandom().randrange(2**31)
        input_hashes = {"reference": _sha(image)} | ({"mask": _sha(mask)} if mask else {})
        if req.stage not in preset.stages:
            raise JobFailed(f"unknown stage {req.stage!r}; available: {', '.join(preset.stages)}")
        mask_source = (
            "TW Mask File"
            if mask
            else "TW Alpha Mask"
            if req.preserve_alpha and has_alpha(image)
            else "TW Remove Background"
        )
        request = {
            "backend": self.url,
            "preset": preset.name,
            "mode": req.mode.value,
            "profile": profile_name,
            "seed": seed,
            "inputs": input_hashes,
            "overrides": req.overrides,
            "image_path": str(image),
            "mask_path": str(mask) if mask else None,
            "display_name": req.display_name or image.stem,
            "parent_revision": req.parent_revision,
            "reference_rights": req.reference_rights,
            "allow_oom_fallback": req.allow_oom_fallback,
            "stage": req.stage,
            "mask_source": mask_source,
        }
        identity = {
            k: request[k]
            for k in ("backend", "preset", "mode", "profile", "seed", "inputs", "overrides", "stage", "mask_source")
        }
        request_sha = workflow.sha256_json(identity)
        for earlier in self.journal.find_request(request_sha):
            if earlier.state not in {JobState.failed, JobState.cancelled, JobState.bundle_failed, JobState.lost}:
                # Same request already in flight or done: reconcile it instead of duplicating work.
                self.progress({"event": "reuse", "job_id": earlier.job_id, "state": earlier.state.value})
                return earlier

        job = Job(
            job_id=uuid.uuid4().hex,
            asset_id=req.asset_id or uuid.uuid4().hex,
            revision=None,
            backend=self.url,
            client_id=uuid.uuid4().hex,
            prompt_id=str(uuid.uuid4()),
            request_sha256=request_sha,
            state=JobState.created,
            request=request,
            detail="",
        )
        self.journal.create(job)  # persisted before anything reaches the backend

        sub = f"trellis-workbench/{job.job_id}"
        uploaded = self.client.upload_image(image.read_bytes(), f"reference{image.suffix.lower()}", sub)
        values: dict[str, object] = {
            "image": f"{uploaded['subfolder']}/{uploaded['name']}",
            "seed": seed,
            "mask_source": mask_source,
        }
        if mask:
            up = self.client.upload_image(mask.read_bytes(), f"mask{mask.suffix.lower()}", sub)
            values["mask_file"] = f"{up['subfolder']}/{up['name']}"
        values |= {k: v for k, v in profile.items() if k in preset.bindings}
        values |= {k: v for k, v in mode.items() if k in preset.bindings}
        values |= {k: v for k, v in req.overrides.items() if k in preset.bindings}
        api = workflow.prune(workflow.bind(preset, values), preset.stages[req.stage])
        for node in api.values():  # per-job output folder: artifacts never collide across jobs
            prefix = node["inputs"].get("filename_prefix")
            if isinstance(prefix, str) and prefix.startswith("trellis-workbench/"):
                node["inputs"]["filename_prefix"] = prefix.replace(
                    "trellis-workbench/", f"trellis-workbench/{job.job_id}/", 1
                )
        problems = workflow.validate(api, self.client.object_info())
        if problems:
            self.journal.set_state(job.job_id, JobState.failed, "stale workflow: " + "; ".join(problems[:5]))
            raise JobFailed("workflow does not match the backend schema:\n  " + "\n  ".join(problems))
        request["workflow"] = api
        request["workflow_sha256"] = workflow.sha256_json(api)
        self.journal.event(job.job_id, "workflow", json.dumps({"sha256": request["workflow_sha256"]}))
        self.journal.set_request(job.job_id, request)
        try:
            self.client.submit(api, job.prompt_id, job.client_id, {"trellis_workbench_job": job.job_id})
        except PromptRejected as exc:
            self.journal.set_state(job.job_id, JobState.failed, f"rejected: {exc}")
            raise JobFailed(f"backend rejected the workflow: {exc}") from exc
        except ComfyError:
            # The POST may or may not have arrived: reconcile by prompt id rather than resubmitting.
            self.journal.set_state(job.job_id, JobState.submitted, "submission response lost")
            return self.journal.get(job.job_id)
        self.journal.set_state(job.job_id, JobState.submitted)
        return self.journal.get(job.job_id)

    # --- observation ------------------------------------------------------------------------

    def follow(self, job_id: str, idle_timeout: float = 900.0, max_reconnects: int = 20) -> Job:
        job = self.journal.get(job_id)
        for _ in range(max_reconnects):
            if job.state in {JobState.published, JobState.failed, JobState.cancelled, JobState.bundle_failed}:
                return job
            where = self.client.locate(job.prompt_id)
            if where == "history":
                return self._finish(job)
            if where == "absent":
                self.journal.set_state(job.job_id, JobState.lost, "not in history or queue (backend restart?)")
                return self.journal.get(job_id)
            if where == "running" and job.state != JobState.running:
                self.journal.set_state(job.job_id, JobState.running)
            try:
                for msg in self.client.events(job.client_id, job.prompt_id, idle_timeout):
                    kind = msg.get("type")
                    data = msg.get("data") or {}
                    if kind == "execution_start":
                        self.journal.set_state(job.job_id, JobState.running)
                    if kind in {"executing", "progress", "execution_start", "execution_cached"}:
                        self.progress({"event": kind, "job_id": job_id, "data": data})
                # Terminal event seen; history is the source of truth for the outcome.
                self.client.wait_history(job.prompt_id, timeout=60)
            except ComfyError as exc:
                self.journal.event(job.job_id, "reconnect", str(exc))
                self.progress({"event": "reconnect", "job_id": job_id, "detail": str(exc)})
            job = self.journal.get(job_id)
        raise JobFailed(f"gave up following job {job_id} after {max_reconnects} reconnects")

    def _finish(self, job: Job) -> Job:
        entry = self.client.history(job.prompt_id)
        outcome, messages = history_outcome(entry)
        if outcome == "interrupted":
            self.journal.set_state(job.job_id, JobState.cancelled, "interrupted")
            return self.journal.get(job.job_id)
        if outcome != "success":
            detail = next((json.dumps(m["data"])[:1500] for m in messages if m["type"] == "execution_error"), outcome)
            self.journal.set_state(job.job_id, JobState.failed, detail)
            if is_oom(messages) and job.request.get("allow_oom_fallback") and job.request["profile"] != "low-memory":
                self.journal.event(job.job_id, "oom", "out of GPU memory; retrying once with profile low-memory")
                return self._oom_retry(job)
            self._free_if_idle()
            return self.journal.get(job.job_id)
        self.journal.set_state(job.job_id, JobState.succeeded)
        try:
            self._publish(job, entry)
        except Exception as exc:
            self.journal.set_state(job.job_id, JobState.bundle_failed, f"{type(exc).__name__}: {exc}")
            raise
        finally:
            self._free_if_idle()
        return self.journal.get(job.job_id)

    def _oom_retry(self, job: Job) -> Job:
        r = job.request
        req = Request(
            image=Path(r["image_path"]),
            mask=Path(r["mask_path"]) if r.get("mask_path") else None,
            mode=Category(r["mode"]),
            profile="low-memory",
            seed=r["seed"],
            display_name=r["display_name"],
            asset_id=job.asset_id,
            parent_revision=r.get("parent_revision"),
            reference_rights=r.get("reference_rights", "unspecified"),
            overrides=r.get("overrides", {}) | {"oom_fallback_from": r["profile"]},
            allow_oom_fallback=False,  # bounded: one lower-memory attempt, never a downward spiral
        )
        preset = workflow.load_preset()
        retry = self.submit(req, preset)
        self.journal.event(job.job_id, "oom-retry", retry.job_id)
        return self.follow(retry.job_id)

    def _free_if_idle(self) -> None:
        try:
            q = self.client.queue()
            if not q.get("queue_running") and not q.get("queue_pending"):
                self.client.free()
        except ComfyError:
            pass

    # --- bundle -----------------------------------------------------------------------------

    def _publish(self, job: Job, entry: dict) -> Path:
        preset = workflow.load_preset()
        r = job.request
        mode = Category(r["mode"])
        refs = output_refs(entry)
        titles = {node.get("_meta", {}).get("title"): nid for nid, node in r["workflow"].items()}
        staging = bundlelib.Staging(self.root, job.asset_id)
        try:
            image = Path(r["image_path"])
            if _sha(image) != r["inputs"]["reference"]:
                raise JobFailed("reference image changed on disk since submission")
            files = [staging.add_file(f"input/reference{image.suffix.lower()}", image, "reference")]
            if r.get("mask_path"):
                files.append(
                    staging.add_file(f"input/mask{Path(r['mask_path']).suffix.lower()}", Path(r["mask_path"]), "mask")
                )
            glb_path = None
            for role, node_titles in preset.outputs.items():
                for title in node_titles:
                    for i, ref in enumerate(refs.get(titles.get(title, ""), [])):
                        suffix = Path(ref.filename).suffix.lower()
                        tag = title.removeprefix("TW Save ").lower().replace(" ", "_")
                        rel = {
                            "working-glb": "working/model.glb",
                            "source-glb": "source/high_detail.glb",
                            "processed-input": f"input/processed{suffix}",
                        }.get(role, f"textures/{tag}{'-' + str(i) if i else ''}{suffix}")
                        rec = staging.add_bytes(rel, self.client.view(ref), role)
                        files.append(rec)
                        if role == "working-glb" or (role == "source-glb" and glb_path is None):
                            glb_path = staging.dir / rec.path
            if glb_path is None:
                raise JobFailed("workflow finished but produced no GLB")
            report = mesh.validate_glb(glb_path, mode)
            files.append(staging.add_bytes("reports/mesh.json", json.dumps(report, indent=2).encode(), "report"))
            log = "\n".join(
                json.dumps({"at": a, "kind": k, "detail": d}) for a, k, d in self.journal.events(job.job_id)
            )
            files.append(staging.add_bytes("logs/job.jsonl", log.encode(), "log"))
            if report["result"] == "error":
                raise JobFailed(
                    "generated mesh failed validation: "
                    + "; ".join(f["detail"] for f in report["findings"] if f["severity"] == "error")
                )

            stats = self.client.system_stats()
            profile = preset.profiles[r["profile"]]
            used_models = sorted(
                {
                    v
                    for node in r["workflow"].values()
                    for v in node["inputs"].values()
                    if isinstance(v, str) and v in MODEL_LICENSES
                }
            )
            events = (
                [Event(at=now(), kind="oom-fallback", detail=f"from profile {r['overrides']['oom_fallback_from']}")]
                if "oom_fallback_from" in r.get("overrides", {})
                else []
            )
            revs = bundlelib.revisions(self.root, job.asset_id)
            manifest_kwargs = {
                "asset_id": job.asset_id,
                "parent_revision": r.get("parent_revision")
                if r.get("parent_revision") is not None
                else (revs[-1] if revs else None),
                "display_name": r["display_name"],
                "category": mode,
                "intended_use": preset.modes[mode.value]["intended_use"],
                "created": now(),
                "job_id": job.job_id,
                "prompt_id": job.prompt_id,
                "workflow_sha256": r["workflow_sha256"],
                "input_hashes": r["inputs"],
                "settings": GenerationSettings(
                    mode=mode,
                    profile=r["profile"],
                    seed=r["seed"],
                    resolution=int(profile["resolution"]),
                    texture_size=int(profile["texture_size"]),
                    face_budget=int(profile["face_budget"]),
                    diffusion_model=profile["diffusion_model"],
                    precision=profile["precision"],
                    overrides=dict(r.get("overrides", {})),
                ),
                "runtime": runtime_of(self.url, stats),
                "files": files,
                "validation": {"result": report["result"], "stats": report["stats"]},
                "licensing": Licensing(
                    profile=self.cfg.license_profile,
                    components=[
                        ModelRef(filename=m, folder=MODEL_LICENSES[m][0], license=MODEL_LICENSES[m][1])
                        for m in used_models
                    ],
                    reference_rights=r.get("reference_rights", "unspecified"),
                ),
                "events": events,
            }
            for _attempt in range(5):  # another writer may claim a revision number concurrently
                rev = (bundlelib.revisions(self.root, job.asset_id) or [0])[-1] + 1
                try:
                    out = staging.publish(Manifest(revision=rev, **manifest_kwargs))
                    break
                except bundlelib.BundleError as exc:
                    if "already exists" not in str(exc):
                        raise
                    (staging.dir / "manifest.json").unlink(missing_ok=True)
            else:
                raise JobFailed("could not claim a free revision number")
        except BaseException:
            staging.discard()
            raise
        self.journal.set_revision(job.job_id, rev)
        self.journal.set_state(job.job_id, JobState.published, str(out))
        self.progress({"event": "published", "job_id": job.job_id, "path": str(out)})
        return out

    # --- cancellation -----------------------------------------------------------------------

    def cancel(self, job_id: str) -> str:
        """Cancel only this job's prompt: dequeue it if pending, or interrupt it only while it
        is the running prompt. Never a global interrupt."""
        job = self.journal.get(job_id)
        where = self.client.locate(job.prompt_id)
        if where == "pending":
            self.client.delete_pending(job.prompt_id)
            if self.client.locate(job.prompt_id) == "absent":
                self.journal.set_state(job_id, JobState.cancelled, "removed from queue")
                return "dequeued"
            where = self.client.locate(job.prompt_id)
        if where == "running":
            self.client.interrupt(job.prompt_id)  # server re-checks ownership of the running prompt
            entry = self.client.wait_history(job.prompt_id, timeout=120)
            outcome, _ = history_outcome(entry)
            if outcome == "interrupted":
                self.journal.set_state(job_id, JobState.cancelled, "interrupted while running")
                return "interrupted"
            return f"finished before the interrupt took effect ({outcome})"
        if where == "history":
            return "already finished; nothing to cancel"
        self.journal.set_state(job_id, JobState.cancelled, "absent from backend")
        return "absent"
