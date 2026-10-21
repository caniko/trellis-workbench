"""Shared fixtures: a fake ComfyUI server and procedural GLB assets (no models, no GPU)."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import pytest
from aiohttp import WSMsgType, web
from assets import png_bytes, textured_box_glb

from trellis_workbench import workflow


def schema_for(api: dict) -> dict:
    """An /object_info covering exactly the shipped workflow (used where the live one is absent)."""
    info: dict = {}
    for node in api.values():
        spec = info.setdefault(node["class_type"], {"input": {"required": {}, "optional": {}}})
        for name, value in node["inputs"].items():
            base = name.split(".", 1)[0]
            if isinstance(value, list):
                spec["input"]["required"][base] = ["*", {}]
            elif base not in spec["input"]["required"]:
                spec["input"]["required"][base] = ["STRING" if isinstance(value, str) else "FLOAT", {}]
    preset = workflow.load_preset()
    models = {p["diffusion_model"] for p in preset.profiles.values()}
    info["UNETLoader"]["input"]["required"]["unet_name"] = [sorted(models), {}]
    return info


class FakeComfy:
    """Implements the ComfyUI routes the bridge uses, with a test-driven executor."""

    def __init__(self):
        self.pending: list[dict] = []
        self.running: dict | None = None
        self.history: dict[str, dict] = {}
        self.ws: dict[str, list[web.WebSocketResponse]] = {}
        self.posts: list[dict] = []
        self.interrupts: list[dict] = []
        self.frees = 0
        self.uploads: dict[str, bytes] = {}
        self.files: dict[tuple[str, str], bytes] = {}
        self.number = 0
        self.device = {"name": "fake-gpu", "type": "cuda", "vram_total": 24 * 2**30, "vram_free": 22 * 2**30}
        self.object_info = schema_for(workflow.load_preset().api)
        self.drop_prompt_response = False
        self.cached_vram = 0  # models ComfyUI keeps loaded until /free
        self.loop: asyncio.AbstractEventLoop | None = None

    # executor controls (called from the test thread)
    def call(self, fn, *args):
        return asyncio.run_coroutine_threadsafe(fn(*args), self.loop).result(10)

    async def _emit(self, client_id, typ, data):
        for ws in list(self.ws.get(client_id, [])):
            if not ws.closed:
                await ws.send_str(json.dumps({"type": typ, "data": data}))

    async def start_next(self):
        item = self.pending.pop(0)
        self.running = item
        await self._emit(item["client_id"], "execution_start", {"prompt_id": item["prompt_id"]})
        await self._emit(item["client_id"], "executing", {"node": "1", "prompt_id": item["prompt_id"]})
        return item["prompt_id"]

    async def finish(self, outcome="success", glb: bytes | None = None, error=None):
        item, self.running = self.running, None
        pid, prompt = item["prompt_id"], item["prompt"]
        outputs = {}
        if outcome == "success":
            for nid, node in prompt.items():
                prefix = node["inputs"].get("filename_prefix")
                if prefix is None:
                    continue
                if node["class_type"] == "SaveGLB":
                    name, data = f"{Path(prefix).name}_00001_.glb", glb or textured_box_glb()
                    outputs[nid] = {"3d": [{"filename": name, "subfolder": str(Path(prefix).parent), "type": "output"}]}
                else:
                    name, data = f"{Path(prefix).name}_00001_.png", png_bytes()
                    outputs[nid] = {
                        "images": [{"filename": name, "subfolder": str(Path(prefix).parent), "type": "output"}]
                    }
                self.files[(str(Path(prefix).parent), name)] = data
            msgs = [["execution_start", {"prompt_id": pid}], ["execution_success", {"prompt_id": pid}]]
            status = {"status_str": "success", "completed": True, "messages": msgs}
        elif outcome == "interrupted":
            msgs = [["execution_start", {"prompt_id": pid}], ["execution_interrupted", {"prompt_id": pid}]]
            status = {"status_str": "error", "completed": False, "messages": msgs}
        else:
            err = error or {
                "prompt_id": pid,
                "exception_type": "torch.OutOfMemoryError",
                "exception_message": "HIP out of memory. Tried to allocate 2.00 GiB",
            }
            msgs = [["execution_start", {"prompt_id": pid}], ["execution_error", err]]
            status = {"status_str": "error", "completed": False, "messages": msgs}
        self.history[pid] = {"prompt": [0, pid, prompt, {}, []], "outputs": outputs, "status": status}
        typ = {"success": "execution_success", "interrupted": "execution_interrupted"}.get(outcome, "execution_error")
        await self._emit(item["client_id"], typ, msgs[-1][1])

    async def drop_websockets(self):
        for socks in self.ws.values():
            for ws in socks:
                await ws.close()

    # routes
    def app(self) -> web.Application:
        r = web.RouteTableDef()

        @r.get("/system_stats")
        async def stats(_req):
            return web.json_response(
                {
                    "system": {"comfyui_version": "0.37.0-fake", "pytorch_version": "2.13.0", "python_version": "3.14"},
                    "devices": [self.device],
                }
            )

        @r.get("/object_info")
        async def info(_req):
            return web.json_response(self.object_info)

        @r.post("/upload/image")
        async def upload(req):
            form = await req.post()
            f = form["image"]
            sub = form.get("subfolder", "")
            self.uploads[f"{sub}/{f.filename}"] = f.file.read()
            return web.json_response({"name": f.filename, "subfolder": sub, "type": "input"})

        @r.post("/prompt")
        async def prompt(req):
            body = await req.json()
            self.posts.append(body)
            pid = body["prompt_id"]
            if pid in self.history or any(i["prompt_id"] == pid for i in self.pending):
                return web.json_response({"error": "duplicate"}, status=400)
            self.number += 1
            self.pending.append({"prompt_id": pid, "prompt": body["prompt"], "client_id": body["client_id"]})
            if self.drop_prompt_response:
                self.drop_prompt_response = False
                raise web.HTTPInternalServerError()
            return web.json_response({"prompt_id": pid, "number": self.number, "node_errors": {}})

        @r.get("/queue")
        async def queue(_req):
            run = [[0, self.running["prompt_id"], {}, {}, []]] if self.running else []
            pend = [[i, p["prompt_id"], {}, {}, []] for i, p in enumerate(self.pending)]
            return web.json_response({"queue_running": run, "queue_pending": pend})

        @r.post("/queue")
        async def queue_post(req):
            body = await req.json()
            self.pending = [p for p in self.pending if p["prompt_id"] not in body.get("delete", [])]
            return web.Response()

        @r.post("/interrupt")
        async def interrupt(req):
            body = await req.json()
            self.interrupts.append(body)
            pid = body.get("prompt_id")
            if self.running and (pid is None or pid == self.running["prompt_id"]):
                await self.finish("interrupted")
            return web.Response()

        @r.post("/free")
        async def free(_req):
            self.frees += 1
            self.device["vram_free"] += self.cached_vram
            self.cached_vram = 0
            return web.Response()

        @r.get("/history/{pid}")
        async def history(req):
            pid = req.match_info["pid"]
            return web.json_response({pid: self.history[pid]} if pid in self.history else {})

        @r.get("/view")
        async def view(req):
            key = (req.query["subfolder"], req.query["filename"])
            if key not in self.files:
                raise web.HTTPNotFound()
            return web.Response(body=self.files[key])

        @r.get("/ws")
        async def ws(req):
            sock = web.WebSocketResponse()
            await sock.prepare(req)
            self.ws.setdefault(req.query["clientId"], []).append(sock)
            async for msg in sock:
                if msg.type == WSMsgType.ERROR:
                    break
            return sock

        app = web.Application()
        app.add_routes(r)
        return app


@pytest.fixture
def fake_comfy():
    fake = FakeComfy()
    started = threading.Event()
    holder = {}

    def run():
        loop = asyncio.new_event_loop()
        fake.loop = loop
        runner = web.AppRunner(fake.app())
        loop.run_until_complete(runner.setup())
        site = web.TCPSite(runner, "127.0.0.1", 0)
        loop.run_until_complete(site.start())
        holder["port"] = site._server.sockets[0].getsockname()[1]
        holder["runner"] = runner
        started.set()
        loop.run_forever()
        loop.run_until_complete(runner.cleanup())

    t = threading.Thread(target=run, daemon=True)
    t.start()
    started.wait(10)
    fake.url = f"http://127.0.0.1:{holder['port']}"
    yield fake
    fake.loop.call_soon_threadsafe(fake.loop.stop)
    t.join(5)


@pytest.fixture
def cfg(tmp_path, fake_comfy):
    from trellis_workbench.config import Config

    root = tmp_path / "assets"
    return Config(
        backends={"fake": fake_comfy.url},
        default_backend="fake",
        asset_root=root,
        allowed_roots=(tmp_path,),
        license_profile="mixed",
        journal=tmp_path / "journal.sqlite",
    )


@pytest.fixture
def reference(tmp_path) -> Path:
    p = tmp_path / "ref.png"
    p.write_bytes(png_bytes(mode="RGB", color=(10, 200, 30)))
    return p
