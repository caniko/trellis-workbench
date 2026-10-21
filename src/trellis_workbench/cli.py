"""trellis-workbench command line."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import bundle as bundlelib
from . import config as configlib
from . import workflow
from .comfy import ComfyClient, ComfyError
from .jobs import JobFailed, Request, Runner, accelerator_or_fail
from .journal import Journal
from .schema import Category, JobState

REQUIRED_MODELS = {
    "clip_vision": ["dino_v3_vit_l.safetensors"],
    "background_removal": ["birefnet.safetensors"],
}


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True, default=str))


def _progress(event: dict) -> None:
    kind = event.get("event")
    if kind == "progress":
        d = event["data"]
        print(f"  {d.get('node', '?')}: {d.get('value')}/{d.get('max')}", file=sys.stderr)
    elif kind == "executing" and event["data"].get("node"):
        print(f"  executing node {event['data']['node']}", file=sys.stderr)
    elif kind in {"reconnect", "reuse", "published"}:
        print(f"  {kind}: {json.dumps({k: v for k, v in event.items() if k != 'event'})}", file=sys.stderr)


def cmd_doctor(cfg, args) -> int:
    report = {
        "config": {
            "asset_root": str(cfg.asset_root),
            "journal": str(cfg.journal),
            "license_profile": cfg.license_profile,
            "backends": cfg.backends,
        },
        "checks": [],
    }

    def check(name, fn):
        try:
            report["checks"].append({"name": name, "status": "passed", "detail": fn()})
        except Exception as exc:  # noqa: BLE001 - report everything
            report["checks"].append({"name": name, "status": "failed", "detail": f"{type(exc).__name__}: {exc}"})

    check("asset-root", lambda: str(bundlelib.check_root(cfg.asset_root, cfg.roots())))
    check("journal", lambda: str(Journal(cfg.journal).path))
    url = cfg.backend_url(args.backend)
    client = ComfyClient(url)
    stats: dict = {}

    def backend():
        stats.update(client.system_stats())
        dev = accelerator_or_fail(stats)
        return {
            "url": url,
            "comfyui": stats["system"].get("comfyui_version"),
            "pytorch": stats["system"].get("pytorch_version"),
            "device": dev.get("name"),
            "type": dev.get("type"),
            "vram_free_gib": round(dev.get("vram_free", 0) / 2**30, 2),
            "vram_total_gib": round(dev.get("vram_total", 0) / 2**30, 2),
        }

    check("backend-gpu", backend)
    info: dict = {}

    def schema():
        info.update(client.object_info())
        preset = workflow.load_preset()
        missing = sorted({n["class_type"] for n in preset.api.values()} - set(info))
        if missing:
            raise RuntimeError(f"backend lacks nodes {missing}")
        return {"nodes": len(info)}

    check("node-schema", schema)

    def models():
        found = {
            "clip_vision": workflow._options(info["CLIPVisionLoader"]["input"]["required"]["clip_name"]) or [],
            "background_removal": workflow._options(
                info["LoadBackgroundRemovalModel"]["input"]["required"]["bg_removal_name"]
            )
            or [],
            "diffusion_models": workflow._options(info["UNETLoader"]["input"]["required"]["unet_name"]) or [],
            "vae": workflow._options(info["VAELoader"]["input"]["required"]["vae_name"]) or [],
        }
        preset = workflow.load_preset()
        need = {k: set(v) for k, v in REQUIRED_MODELS.items()}
        need.setdefault("diffusion_models", set()).update(p["diffusion_model"] for p in preset.profiles.values())
        need["vae"] = {"trellis_2_shape_vae_bf16.safetensors", "trellis_2_texture_vae_bf16.safetensors"}
        missing = {k: sorted(v - set(found[k])) for k, v in need.items() if v - set(found[k])}
        if missing:
            raise RuntimeError(f"models not provisioned on the backend: {missing}")
        return "all profile models present"

    if info:
        check("models", models)

        def workflows():
            problems = {k: v for k, v in validate_all(info).items() if v}
            if problems:
                raise RuntimeError(json.dumps(problems)[:3000])
            return "all stage/mode/profile combinations match the backend schema"

        check("workflows", workflows)
    report["ok"] = all(c["status"] == "passed" for c in report["checks"])
    _print(report)
    return 0 if report["ok"] else 1


def validate_all(info: dict) -> dict[str, list[str]]:
    """Problems per stage/mode/profile combination of the shipped workflow."""
    preset = workflow.load_preset()
    out = {}
    for stage in preset.stages:
        for mode, mvals in preset.modes.items():
            for prof, pvals in preset.profiles.items():
                values = {k: v for k, v in (pvals | mvals).items() if k in preset.bindings}
                api = workflow.prune(workflow.bind(preset, values), preset.stages[stage])
                out[f"{stage}/{mode}/{prof}"] = workflow.validate(api, info)
    return out


def cmd_workflows_validate(cfg, args) -> int:
    info = (
        json.loads(Path(args.object_info).read_text())
        if args.object_info
        else ComfyClient(cfg.backend_url(args.backend)).object_info()
    )
    results = validate_all(info)
    for combo, problems in results.items():
        print(f"{combo:32} {'ok' if not problems else 'STALE'}")
        for p in problems:
            print(f"    {p}")
    return 1 if any(results.values()) else 0


def cmd_object_info(cfg, args) -> int:
    Path(args.out).write_text(json.dumps(ComfyClient(cfg.backend_url(args.backend)).object_info(), sort_keys=True))
    return 0


def _request(args) -> Request:
    return Request(
        image=Path(args.image),
        mask=Path(args.mask) if args.mask else None,
        mode=Category(args.mode),
        profile=args.profile,
        seed=args.seed,
        display_name=args.name,
        asset_id=args.asset_id,
        reference_rights=args.reference_rights,
        stage=args.stage,
        preserve_alpha=not args.ignore_alpha,
        allow_oom_fallback=not args.no_oom_fallback,
    )


def cmd_submit(cfg, args) -> int:
    runner = Runner(cfg, args.backend, _progress)
    preset = workflow.load_preset()
    try:
        job = runner.submit(_request(args), preset)
        print(f"job {job.job_id} prompt {job.prompt_id} asset {job.asset_id}", file=sys.stderr)
        if not args.no_wait:
            job = runner.follow(job.job_id)
    except (JobFailed, ComfyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3 if str(exc).startswith("deferred:") else 1
    _print({"job_id": job.job_id, "state": job.state.value, "detail": job.detail, "asset_id": job.asset_id})
    return 0 if job.state in {JobState.published, JobState.submitted, JobState.running} else 1


def cmd_status(cfg, args) -> int:
    journal = Journal(cfg.journal)
    if not args.job_id:
        for j in journal.list(args.limit):
            print(
                f"{j.job_id}  {j.state.value:13} {j.request.get('mode', '?'):9} {j.request.get('display_name', '')}  {j.detail[:80]}"
            )
        return 0
    job = journal.get(args.job_id)
    if args.watch and job.state not in {
        JobState.published,
        JobState.failed,
        JobState.cancelled,
        JobState.bundle_failed,
    }:
        job = Runner(cfg, job.backend, _progress).follow(job.job_id)
    elif args.reconcile:
        where = ComfyClient(job.backend).locate(job.prompt_id)
        print(f"backend reports prompt {job.prompt_id}: {where}", file=sys.stderr)
    _print(
        {
            "job": job.__dict__
            | {"state": job.state.value, "request": {k: v for k, v in job.request.items() if k != "workflow"}},
            "events": journal.events(job.job_id),
        }
    )
    return 0


def cmd_cancel(cfg, args) -> int:
    job = Journal(cfg.journal).get(args.job_id)
    print(Runner(cfg, job.backend).cancel(args.job_id))
    return 0


def cmd_bundle(cfg, args) -> int:
    if args.bundle_cmd == "list":
        _print(bundlelib.list_bundles(cfg.asset_root))
        return 0
    try:
        m = bundlelib.verify(Path(args.path))
    except (bundlelib.BundleError, ValueError, OSError) as exc:
        print(f"invalid bundle: {exc}", file=sys.stderr)
        return 1
    _print(json.loads(m.model_dump_json()))
    return 0


def cmd_bench(cfg, args) -> int:
    """Sequential jobs with wall time and the lowest free VRAM the backend reported."""
    out = Path(args.out)
    for i in range(args.runs):
        runner = Runner(cfg, args.backend, lambda _e: None)
        client = runner.client
        start = time.monotonic()
        before = (client.system_stats().get("devices") or [{}])[0]
        req = _request(args)
        req.seed = args.seed if args.seed is not None else i
        job = runner.submit(req, workflow.load_preset())
        min_free = before.get("vram_free")
        while (where := client.locate(job.prompt_id)) in {"pending", "running"}:
            dev = (client.system_stats().get("devices") or [{}])[0]
            min_free = min(min_free, dev.get("vram_free", min_free))
            time.sleep(2)
        job = runner.follow(job.job_id)
        record = {
            "run": i,
            "cold": i == 0,
            "job_id": job.job_id,
            "state": job.state.value,
            "where": where,
            "profile": args.profile,
            "stage": args.stage,
            "mode": args.mode,
            "wall_s": round(time.monotonic() - start, 1),
            "vram_total": before.get("vram_total"),
            "vram_free_before": before.get("vram_free"),
            "vram_free_min_observed": min_free,
            "detail": job.detail,
        }
        with out.open("a") as f:
            f.write(json.dumps(record) + "\n")
        print(json.dumps(record))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="trellis-workbench")
    p.add_argument("--config", type=Path)
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("doctor", help="check configuration, backend GPU, node schema, models and workflows")
    d.add_argument("--backend")

    w = sub.add_parser("workflows", help="workflow utilities")
    wsub = w.add_subparsers(dest="workflows_cmd", required=True)
    wv = wsub.add_parser("validate", help="validate every shipped stage/mode/profile against a node schema")
    wv.add_argument("--backend")
    wv.add_argument("--object-info", help="recorded /object_info JSON instead of a live backend")
    wo = wsub.add_parser("object-info", help="record the backend's /object_info")
    wo.add_argument("--backend")
    wo.add_argument("out")

    def job_args(sp):
        sp.add_argument("--backend")
        sp.add_argument("--image", required=True)
        sp.add_argument("--mask")
        sp.add_argument("--mode", required=True, choices=[c.value for c in Category])
        sp.add_argument("--profile", default="standard")
        sp.add_argument("--stage", default="full", choices=["shape", "full"])
        sp.add_argument("--seed", type=int)
        sp.add_argument("--name")
        sp.add_argument("--asset-id")
        sp.add_argument(
            "--reference-rights",
            default="unspecified",
            help="who holds rights to the reference image (recorded, not verified)",
        )
        sp.add_argument("--ignore-alpha", action="store_true", help="remove the background even for RGBA input")
        sp.add_argument("--no-oom-fallback", action="store_true")

    s = sub.add_parser("submit", help="generate an asset bundle from a reference image")
    job_args(s)
    s.add_argument("--no-wait", action="store_true")

    st = sub.add_parser("status", help="list jobs or show/reconcile one")
    st.add_argument("job_id", nargs="?")
    st.add_argument("--watch", action="store_true")
    st.add_argument("--reconcile", action="store_true")
    st.add_argument("--limit", type=int, default=20)

    c = sub.add_parser("cancel", help="cancel one job (dequeue, or interrupt only if it is running)")
    c.add_argument("job_id")

    b = sub.add_parser("bundle", help="inspect published bundles")
    bsub = b.add_subparsers(dest="bundle_cmd", required=True)
    bsub.add_parser("list")
    bi = bsub.add_parser("inspect", help="verify checksums and print the manifest")
    bi.add_argument("path")

    be = sub.add_parser("bench", help="run sequential jobs and record timings")
    job_args(be)
    be.add_argument("--runs", type=int, default=2)
    be.add_argument("--out", default="bench.jsonl")

    sub.add_parser("serve-addon", help="JSON-lines helper for the Blender add-on (stdin/stdout)")

    args = p.parse_args(argv)
    cfg = configlib.load(args.config)
    if args.cmd == "doctor":
        return cmd_doctor(cfg, args)
    if args.cmd == "workflows":
        return cmd_workflows_validate(cfg, args) if args.workflows_cmd == "validate" else cmd_object_info(cfg, args)
    if args.cmd == "submit":
        return cmd_submit(cfg, args)
    if args.cmd == "status":
        return cmd_status(cfg, args)
    if args.cmd == "cancel":
        return cmd_cancel(cfg, args)
    if args.cmd == "bundle":
        return cmd_bundle(cfg, args)
    if args.cmd == "bench":
        return cmd_bench(cfg, args)
    if args.cmd == "serve-addon":
        from .addon_server import serve

        return serve(cfg)
    return 2


if __name__ == "__main__":
    sys.exit(main())
