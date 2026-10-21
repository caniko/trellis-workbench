"""Minimal client for ComfyUI's documented server routes.

https://docs.comfy.org/development/comfyui-server/comms_routes
Only HTTP/JSON and the /ws event stream are used; no ComfyUI Python objects cross over.
"""

from __future__ import annotations

import json
import mimetypes
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import PurePosixPath

from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

TERMINAL_EVENTS = {"execution_success", "execution_error", "execution_interrupted"}


class ComfyError(RuntimeError):
    pass


class PromptRejected(ComfyError):
    """ComfyUI refused the prompt at validation time (HTTP 400)."""

    def __init__(self, payload: dict):
        super().__init__(json.dumps(payload)[:2000])
        self.payload = payload


@dataclass(frozen=True)
class OutputRef:
    filename: str
    subfolder: str
    type: str

    def checked(self) -> OutputRef:
        """Reject anything but a plain file inside ComfyUI's output/temp tree."""
        if self.type not in {"output", "temp"}:
            raise ComfyError(f"unsupported artifact type {self.type!r}")
        name = PurePosixPath(self.filename)
        if self.filename in {"", ".", ".."} or len(name.parts) != 1 or "\\" in self.filename or "\0" in self.filename:
            raise ComfyError(f"unsafe artifact filename {self.filename!r}")
        sub = PurePosixPath(self.subfolder)
        if sub.is_absolute() or ".." in sub.parts or "\\" in self.subfolder or "\0" in self.subfolder:
            raise ComfyError(f"unsafe artifact subfolder {self.subfolder!r}")
        return self


class ComfyClient:
    def __init__(self, base_url: str, timeout: float = 30.0):
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"backend URL must be http(s)://host:port, got {base_url!r}")
        self.base = base_url.rstrip("/")
        self.timeout = timeout

    # --- HTTP -------------------------------------------------------------------------------

    def _request(self, method: str, path: str, body: bytes | None = None, headers: dict | None = None) -> bytes:
        req = urllib.request.Request(self.base + path, data=body, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read()
            if exc.code == 400 and path == "/prompt":
                try:
                    raise PromptRejected(json.loads(detail)) from exc
                except json.JSONDecodeError:
                    pass
            raise ComfyError(f"{method} {path}: HTTP {exc.code}: {detail[:500]!r}") from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise ComfyError(f"{method} {path}: backend unreachable: {exc}") from exc

    def _json(self, method: str, path: str, payload: dict | None = None):
        body = None if payload is None else json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"} if body is not None else {}
        raw = self._request(method, path, body, headers)
        return json.loads(raw) if raw.strip() else None

    def system_stats(self) -> dict:
        return self._json("GET", "/system_stats")

    def object_info(self) -> dict:
        return self._json("GET", "/object_info")

    def queue(self) -> dict:
        return self._json("GET", "/queue")

    def history(self, prompt_id: str) -> dict | None:
        return self._json("GET", f"/history/{urllib.parse.quote(prompt_id)}").get(prompt_id)

    def submit(self, prompt: dict, prompt_id: str, client_id: str, extra_data: dict | None = None) -> dict:
        # A client-chosen prompt_id makes resubmission after a lost connection detectable.
        payload = {"prompt": prompt, "prompt_id": prompt_id, "client_id": client_id, "extra_data": extra_data or {}}
        return self._json("POST", "/prompt", payload)

    def delete_pending(self, prompt_id: str) -> None:
        self._json("POST", "/queue", {"delete": [prompt_id]})

    def interrupt(self, prompt_id: str) -> None:
        # Targeted form: the server interrupts only if this prompt is the running one.
        self._json("POST", "/interrupt", {"prompt_id": prompt_id})

    def free(self) -> None:
        self._json("POST", "/free", {"unload_models": True, "free_memory": True})

    def upload_image(self, data: bytes, filename: str, subfolder: str) -> dict:
        boundary = uuid.uuid4().hex
        ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        parts = [
            (f'Content-Disposition: form-data; name="image"; filename="{filename}"\r\nContent-Type: {ctype}', data),
            ('Content-Disposition: form-data; name="subfolder"', subfolder.encode()),
            ('Content-Disposition: form-data; name="type"', b"input"),
            ('Content-Disposition: form-data; name="overwrite"', b"false"),
        ]
        body = b"".join(f"--{boundary}\r\n{head}\r\n\r\n".encode() + value + b"\r\n" for head, value in parts)
        body += f"--{boundary}--\r\n".encode()
        raw = self._request(
            "POST", "/upload/image", body, {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        )
        return json.loads(raw)

    def view(self, ref: OutputRef) -> bytes:
        ref.checked()
        query = urllib.parse.urlencode({"filename": ref.filename, "subfolder": ref.subfolder, "type": ref.type})
        return self._request("GET", f"/view?{query}")

    # --- job status ---------------------------------------------------------------------------

    def locate(self, prompt_id: str) -> str:
        """Where the prompt is now: 'history', 'running', 'pending' or 'absent'."""
        if self.history(prompt_id) is not None:
            return "history"
        q = self.queue()
        if any(item[1] == prompt_id for item in q.get("queue_running", [])):
            return "running"
        if any(item[1] == prompt_id for item in q.get("queue_pending", [])):
            return "pending"
        # Re-check history: the prompt may have finished between the two requests.
        return "history" if self.history(prompt_id) is not None else "absent"

    def events(self, client_id: str, prompt_id: str, idle_timeout: float) -> Iterator[dict]:
        """Yield this prompt's websocket events until a terminal one. Raises ComfyError on
        disconnect or idle timeout; callers then reconcile through history."""
        ws_url = self.base.replace("http", "ws", 1) + "/ws?" + urllib.parse.urlencode({"clientId": client_id})
        try:
            with connect(ws_url, open_timeout=self.timeout, max_size=None) as ws:
                while True:
                    try:
                        raw = ws.recv(timeout=idle_timeout)
                    except TimeoutError as exc:
                        raise ComfyError(f"no backend event for {idle_timeout}s") from exc
                    if isinstance(raw, bytes):
                        continue  # binary preview frames
                    msg = json.loads(raw)
                    data = msg.get("data") or {}
                    if data.get("prompt_id") not in (None, prompt_id):
                        continue
                    yield msg
                    if msg.get("type") in TERMINAL_EVENTS and data.get("prompt_id") == prompt_id:
                        return
        except (WebSocketException, OSError) as exc:
            raise ComfyError(f"event stream lost: {exc}") from exc

    def wait_history(self, prompt_id: str, timeout: float, poll: float = 2.0, sleep: Callable = time.sleep) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            entry = self.history(prompt_id)
            if entry is not None and entry.get("status", {}).get("completed") is not None:
                return entry
            sleep(poll)
        raise ComfyError(f"prompt {prompt_id} did not reach history within {timeout}s")


def history_outcome(entry: dict) -> tuple[str, list[dict]]:
    """('success'|'error'|'interrupted'|'unknown', messages) from a /history entry."""
    status = entry.get("status") or {}
    messages = [{"type": m[0], "data": m[1]} for m in status.get("messages", []) if isinstance(m, list) and len(m) == 2]
    kinds = {m["type"] for m in messages}
    if "execution_interrupted" in kinds:
        return "interrupted", messages
    if "execution_error" in kinds or status.get("status_str") == "error":
        return "error", messages
    if "execution_success" in kinds or (status.get("status_str") == "success" and status.get("completed")):
        return "success", messages
    return "unknown", messages


def is_oom(messages: list[dict]) -> bool:
    for m in messages:
        if m["type"] != "execution_error":
            continue
        text = " ".join(str(m["data"].get(k, "")) for k in ("exception_type", "exception_message")).lower()
        if "outofmemory" in text.replace(" ", "") or "out of memory" in text:
            return True
    return False


def output_refs(entry: dict) -> dict[str, list[OutputRef]]:
    """Artifact references per node id from a /history entry, each path-checked."""
    refs: dict[str, list[OutputRef]] = {}
    for node_id, out in (entry.get("outputs") or {}).items():
        for items in out.values():
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict) and "filename" in item:
                    ref = OutputRef(
                        str(item["filename"]), str(item.get("subfolder", "")), str(item.get("type", "output"))
                    )
                    refs.setdefault(node_id, []).append(ref.checked())
    return refs
