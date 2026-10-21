"""JSON-lines helper process for the Blender add-on.

Blender spawns `trellis-workbench serve-addon` and exchanges one JSON object per line over
stdin/stdout. All network and long-running work happens here, in a separate Python with its
own dependencies; Blender only parses replies from a timer on its main thread.
"""

from __future__ import annotations

import sys
import threading
import traceback
from pathlib import Path

from pydantic import ValidationError

from . import bundle as bundlelib
from .config import Config
from .jobs import JobFailed, Request, Runner
from .journal import Journal
from .schema import AddonReply, AddonRequest, Category

_out_lock = threading.Lock()


def _send(reply: AddonReply) -> None:
    with _out_lock:
        sys.stdout.write(reply.model_dump_json() + "\n")
        sys.stdout.flush()


def _ok(req_id, data, kind="result"):
    _send(AddonReply(id=req_id, ok=True, kind=kind, data=data))


def _err(req_id, message):
    _send(AddonReply(id=req_id, ok=False, kind="error", data={"message": message}))


def _submit(cfg: Config, req_id: str, args: dict) -> None:
    def progress(event: dict) -> None:
        _ok(None, {"request": req_id} | event, kind="progress")

    try:
        runner = Runner(cfg, args.get("backend"), progress)
        request = Request(
            image=Path(args["image"]),
            mask=Path(args["mask"]) if args.get("mask") else None,
            mode=Category(args["mode"]),
            profile=args.get("profile", "standard"),
            seed=args.get("seed"),
            display_name=args.get("name"),
            asset_id=args.get("asset_id"),
            reference_rights=args.get("reference_rights", "unspecified"),
            stage=args.get("stage", "full"),
        )
        from . import workflow

        job = runner.submit(request, workflow.load_preset())
        _ok(None, {"request": req_id, "event": "submitted", "job_id": job.job_id}, kind="progress")
        job = runner.follow(job.job_id)
        _ok(req_id, {"job_id": job.job_id, "state": job.state.value, "detail": job.detail})
    except (JobFailed, Exception) as exc:  # noqa: BLE001 - every failure goes back to Blender
        _err(req_id, f"{type(exc).__name__}: {exc}")


def serve(cfg: Config, stdin=None) -> int:
    stdin = stdin or sys.stdin
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = AddonRequest.model_validate_json(line)
        except ValidationError as exc:
            _err(None, f"invalid request: {exc.errors()[:3]}")
            continue
        try:
            if req.op == "ping":
                _ok(req.id, {"pong": True})
            elif req.op == "shutdown":
                _ok(req.id, {"bye": True})
                return 0
            elif req.op == "list_bundles":
                _ok(req.id, {"bundles": bundlelib.list_bundles(cfg.asset_root), "root": str(cfg.asset_root)})
            elif req.op == "status":
                jobs = Journal(cfg.journal).list(int(req.args.get("limit", 20)))
                _ok(
                    req.id,
                    {
                        "jobs": [
                            {
                                "job_id": j.job_id,
                                "state": j.state.value,
                                "detail": j.detail,
                                "name": j.request.get("display_name"),
                                "mode": j.request.get("mode"),
                            }
                            for j in jobs
                        ]
                    },
                )
            elif req.op == "cancel":
                job = Journal(cfg.journal).get(req.args["job_id"])
                _ok(req.id, {"result": Runner(cfg, job.backend).cancel(job.job_id)})
            elif req.op == "doctor":
                from .comfy import ComfyClient

                stats = ComfyClient(cfg.backend_url(req.args.get("backend"))).system_stats()
                _ok(req.id, {"system": stats.get("system", {}), "devices": stats.get("devices", [])})
            elif req.op == "submit":
                threading.Thread(target=_submit, args=(cfg, req.id, dict(req.args)), daemon=True).start()
        except Exception as exc:  # noqa: BLE001
            _err(req.id, f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=3)}")
    return 0
