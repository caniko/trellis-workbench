"""Job lifecycle against the fake ComfyUI: completion, reconciliation, cancellation, OOM, VRAM."""

from __future__ import annotations

import socket
import threading
import time

import pytest

from trellis_workbench import bundle as bundlelib
from trellis_workbench import workflow
from trellis_workbench.jobs import JobFailed, Request, Runner
from trellis_workbench.schema import Category, JobState


def wait_for(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.02)
    raise AssertionError("condition not reached")


def follow_in_thread(runner, job_id, **kw):
    out = {}

    def run():
        try:
            out["job"] = runner.follow(job_id, **kw)
        except JobFailed as exc:
            out["error"] = exc

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, out


def submit(cfg, reference, **kw):
    runner = Runner(cfg, None)
    req = Request(image=reference, mode=kw.pop("mode", Category.accessory), seed=7, **kw)
    return runner, runner.submit(req, workflow.load_preset())


def test_success_publishes_validated_bundle(cfg, fake_comfy, reference):
    runner, job = submit(cfg, reference)
    t, out = follow_in_thread(runner, job.job_id)
    wait_for(lambda: fake_comfy.ws)  # client connected before execution starts
    fake_comfy.call(fake_comfy.start_next)
    fake_comfy.call(fake_comfy.finish, "success")
    t.join(20)
    done = out["job"]
    assert done.state == JobState.published, done.detail
    bundle_dir = cfg.asset_root / done.asset_id / "r0001"
    m = bundlelib.verify(bundle_dir)
    roles = {f.role for f in m.files}
    assert {"reference", "working-glb", "source-glb", "processed-input", "texture", "report", "log"} <= roles
    assert m.settings.seed == 7 and m.prompt_id == job.prompt_id
    assert m.coordinates.scale_trusted is False
    assert {c.license for c in m.licensing.components} >= {"LicenseRef-DINOv3", "MIT"}
    assert len(fake_comfy.posts) == 1
    assert fake_comfy.frees == 1  # VRAM released after the job when the queue is idle
    # per-job output folder for every save node
    prompt = fake_comfy.posts[0]["prompt"]
    prefixes = [n["inputs"]["filename_prefix"] for n in prompt.values() if "filename_prefix" in n["inputs"]]
    assert prefixes and all(p.startswith(f"trellis-workbench/{job.job_id}/") for p in prefixes)


def test_node_event_is_not_completion(cfg, fake_comfy, reference):
    runner, job = submit(cfg, reference)
    t, out = follow_in_thread(runner, job.job_id, idle_timeout=0.5, max_reconnects=3)
    wait_for(lambda: fake_comfy.ws)
    fake_comfy.call(fake_comfy.start_next)  # emits execution_start + executing only
    t.join(5)
    assert "gave up" in str(out["error"])  # node events alone never complete a job
    fake_comfy.call(fake_comfy.finish, "success")


def test_disconnect_reconciles_without_resubmitting(cfg, fake_comfy, reference):
    runner, job = submit(cfg, reference)
    t, out = follow_in_thread(runner, job.job_id)
    wait_for(lambda: fake_comfy.ws)
    fake_comfy.call(fake_comfy.start_next)
    fake_comfy.call(fake_comfy.drop_websockets)
    time.sleep(0.3)
    fake_comfy.call(fake_comfy.finish, "success")
    t.join(20)
    assert out["job"].state == JobState.published
    assert len(fake_comfy.posts) == 1


def test_lost_submit_response_is_reconciled_not_duplicated(cfg, fake_comfy, reference):
    fake_comfy.drop_prompt_response = True
    runner, job = submit(cfg, reference)
    assert job.state == JobState.submitted and "lost" in job.detail
    # The same request again must reuse the journal entry instead of posting a second prompt.
    _runner2, again = submit(cfg, reference)
    assert again.job_id == job.job_id
    t, out = follow_in_thread(runner, job.job_id)
    wait_for(lambda: fake_comfy.ws)
    fake_comfy.call(fake_comfy.start_next)
    fake_comfy.call(fake_comfy.finish, "success")
    t.join(20)
    assert out["job"].state == JobState.published
    assert len(fake_comfy.posts) == 1


def test_cancel_pending_removes_only_ours(cfg, fake_comfy, reference):
    _r1, theirs = submit(cfg, reference)
    fake_comfy.call(fake_comfy.start_next)  # someone else's job is running
    runner, ours = submit(cfg, reference, profile="low-memory")
    assert runner.cancel(ours.job_id) == "dequeued"
    assert fake_comfy.running["prompt_id"] == theirs.prompt_id  # untouched
    assert fake_comfy.interrupts == []
    fake_comfy.call(fake_comfy.finish, "success")


def test_cancel_running_is_targeted(cfg, fake_comfy, reference):
    runner, job = submit(cfg, reference)
    fake_comfy.call(fake_comfy.start_next)
    assert runner.cancel(job.job_id) == "interrupted"
    assert fake_comfy.interrupts == [{"prompt_id": job.prompt_id}]  # never a global interrupt
    assert runner.journal.get(job.job_id).state == JobState.cancelled


def test_oom_retries_once_with_low_memory(cfg, fake_comfy, reference):
    runner, job = submit(cfg, reference)
    t, out = follow_in_thread(runner, job.job_id)
    wait_for(lambda: fake_comfy.ws)
    fake_comfy.call(fake_comfy.start_next)
    fake_comfy.call(fake_comfy.finish, "oom")
    wait_for(lambda: len(fake_comfy.posts) == 2)
    retry_prompt = fake_comfy.posts[1]["prompt"]
    assert any(n["inputs"].get("unet_name") == "trellis_2_int8_convrot.safetensors" for n in retry_prompt.values())
    wait_for(lambda: len(fake_comfy.ws) == 2)
    fake_comfy.call(fake_comfy.start_next)
    fake_comfy.call(fake_comfy.finish, "oom")  # second OOM: no third attempt
    t.join(20)
    assert out["job"].state == JobState.failed
    assert len(fake_comfy.posts) == 2
    events = [k for _a, k, _d in runner.journal.events(job.job_id)]
    assert "oom" in events and "oom-retry" in events


def test_vram_pressure_defers_without_submitting(cfg, fake_comfy, reference):
    fake_comfy.device["vram_free"] = 2 * 2**30
    with pytest.raises(JobFailed, match="deferred"):
        submit(cfg, reference)
    assert fake_comfy.posts == []


def test_vram_held_by_comfyui_cache_is_freed_not_deferred(cfg, fake_comfy, reference):
    fake_comfy.device["vram_free"] = 2 * 2**30
    fake_comfy.cached_vram = 20 * 2**30
    _runner, job = submit(cfg, reference)
    assert fake_comfy.frees == 1 and len(fake_comfy.posts) == 1, job.detail


def test_cpu_backend_is_refused(cfg, fake_comfy, reference):
    fake_comfy.device["type"] = "cpu"
    with pytest.raises(JobFailed, match="no GPU"):
        submit(cfg, reference)


def test_stale_schema_fails_before_submission(cfg, fake_comfy, reference):
    del fake_comfy.object_info["UnwrapMesh"]["input"]["required"]["weld_distance"]
    with pytest.raises(JobFailed, match="does not match the backend schema"):
        submit(cfg, reference)
    assert fake_comfy.posts == []


def test_rgba_reference_keeps_its_alpha(cfg, fake_comfy, tmp_path):
    from assets import png_bytes

    ref = tmp_path / "cutout.png"
    ref.write_bytes(png_bytes(color=(255, 0, 0, 0)))
    _runner, _job = submit(cfg, ref)
    prompt = fake_comfy.posts[0]["prompt"]
    titles = {n["_meta"]["title"] for n in prompt.values()}
    assert "TW Alpha Mask" in titles and "TW Remove Background" not in titles


def test_offline_only_backend_traffic(cfg, fake_comfy, reference, monkeypatch):
    """After bootstrap nothing but the configured backend is contacted."""
    real = socket.create_connection
    seen = []

    def guarded(addr, *a, **kw):
        seen.append(addr)
        assert addr[0] in {"127.0.0.1", "localhost"}, f"unexpected network access to {addr}"
        return real(addr, *a, **kw)

    monkeypatch.setattr(socket, "create_connection", guarded)
    runner, job = submit(cfg, reference)
    t, out = follow_in_thread(runner, job.job_id)
    wait_for(lambda: fake_comfy.ws)
    fake_comfy.call(fake_comfy.start_next)
    fake_comfy.call(fake_comfy.finish, "success")
    t.join(20)
    assert out["job"].state == JobState.published and seen


def test_strict_foss_profile_refuses_generation(cfg, fake_comfy, reference):
    from dataclasses import replace

    strict = replace(cfg, license_profile="strict-foss")
    with pytest.raises(JobFailed, match="DINOv3"):
        Runner(strict, None).submit(Request(image=reference, mode=Category.accessory), workflow.load_preset())
    assert fake_comfy.posts == []


def test_blas_allocation_failure_counts_as_oom():
    from trellis_workbench.comfy import is_oom

    msg = [
        {
            "type": "execution_error",
            "data": {
                "exception_type": "RuntimeError",
                "exception_message": "CUDA error: HIPBLAS_STATUS_ALLOC_FAILED when calling `hipblasDgetrfBatched(...)`",
            },
        }
    ]
    assert is_oom(msg)
    assert not is_oom(
        [{"type": "execution_error", "data": {"exception_type": "ValueError", "exception_message": "bad"}}]
    )


def test_status_reconcile_marks_prompt_lost_after_backend_restart(cfg, fake_comfy, reference):
    import argparse

    from trellis_workbench.cli import cmd_status
    from trellis_workbench.journal import Journal

    _runner, job = submit(cfg, reference)
    fake_comfy.pending.clear()  # the backend restarted: no queue entry, no history
    cmd_status(cfg, argparse.Namespace(job_id=job.job_id, watch=False, reconcile=True, limit=20))
    assert Journal(cfg.journal).get(job.job_id).state == JobState.lost
