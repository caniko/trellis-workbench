"""On-host GPU doctor. Runs with the ComfyUI service's own Python/torch; stdlib + torch only.

Invoked through the `trellis-workbench-runtime-doctor` wrapper, which sets TRELLIS_WORKBENCH_VENDOR
(rocm|xpu). It deliberately does not import ComfyUI (GPL-3.0) modules; node availability is
checked over HTTP by `trellis-workbench doctor`, and real rasterization/texturing by `bench`.

Prints one JSON object and exits non-zero if any required check fails. It never falls back
to the CPU: a missing accelerator is a failure, not a downgraded success.
"""

from __future__ import annotations

import json
import os
import sys
import time


def _check(results: list[dict], name: str, fn) -> object:
    start = time.perf_counter()
    try:
        value = fn()
        results.append(
            {"name": name, "status": "passed", "detail": value, "seconds": round(time.perf_counter() - start, 3)}
        )
        return value
    except Exception as exc:  # noqa: BLE001 - every failure is reported, not raised
        results.append({"name": name, "status": "failed", "detail": f"{type(exc).__name__}: {exc}"})
        return None


def main() -> int:
    results: list[dict] = []
    vendor = os.environ.get("TRELLIS_WORKBENCH_VENDOR", "unknown")

    def device_nodes() -> dict:
        nodes = ["/dev/kfd"] if vendor == "rocm" else []
        nodes += sorted(f"/dev/dri/{n}" for n in os.listdir("/dev/dri") if n.startswith("renderD"))
        if not nodes or (vendor == "rocm" and not os.path.exists("/dev/kfd")):
            raise RuntimeError(f"required device nodes missing: {nodes}")
        unusable = [n for n in nodes if not os.access(n, os.R_OK | os.W_OK)]
        if unusable:
            raise PermissionError(f"no read/write access to {unusable} (uid={os.getuid()}, groups={os.getgroups()})")
        return {"nodes": nodes}

    _check(results, "device-nodes", device_nodes)

    import torch

    def backend() -> dict:
        info = {"torch": torch.__version__, "hip": getattr(torch.version, "hip", None)}
        if vendor == "rocm":
            # ROCm builds expose the device through the torch.cuda API names.
            if not info["hip"] or not torch.cuda.is_available():
                raise RuntimeError(f"ROCm torch not usable: {info}")
            props = torch.cuda.get_device_properties(0)
            info |= {"device": props.name, "arch": getattr(props, "gcnArchName", None)}
            free, total = torch.cuda.mem_get_info(0)
        elif vendor == "xpu":
            if not torch.xpu.is_available():
                raise RuntimeError(f"XPU torch not usable: {info}")
            props = torch.xpu.get_device_properties(0)
            info |= {"device": props.name, "driver": getattr(props, "driver_version", None)}
            free, total = torch.xpu.mem_get_info(0)
        else:
            raise RuntimeError(f"unknown vendor {vendor!r}")
        info |= {"vram_free_mib": free // 2**20, "vram_total_mib": total // 2**20}
        return info

    info = _check(results, "torch-backend", backend)
    dev = torch.device("cuda" if vendor == "rocm" else "xpu") if info else None

    def matmul() -> dict:
        a = torch.randn(1024, 1024, generator=torch.Generator().manual_seed(0))
        b = torch.randn(1024, 1024, generator=torch.Generator().manual_seed(1))
        got = (a.to(dev) @ b.to(dev)).cpu()
        err = (got - a @ b).abs().max().item()
        if err > 1e-2:
            raise RuntimeError(f"matmul mismatch {err}")
        return {"max_abs_err": err, "device": str(dev)}

    def sparse_ops() -> dict:
        # The gather/scatter-add and SDPA primitives native TRELLIS.2 builds its sparse conv and attention on.
        idx = torch.randint(0, 4096, (65536,), generator=torch.Generator().manual_seed(2))
        val = torch.randn(65536, 32, generator=torch.Generator().manual_seed(3))
        ref = torch.zeros(4096, 32).index_add_(0, idx, val)
        got = torch.zeros(4096, 32, device=dev).index_add_(0, idx.to(dev), val.to(dev)).cpu()
        q = torch.randn(1, 8, 512, 64, device=dev, dtype=torch.bfloat16)
        out = torch.nn.functional.scaled_dot_product_attention(q, q, q)
        torch.accelerator.synchronize()
        return {"index_add_err": (got - ref).abs().max().item(), "sdpa_finite": bool(torch.isfinite(out).all().item())}

    if dev is not None:
        _check(results, "gpu-matmul", matmul)
        _check(results, "gpu-sparse-primitives", sparse_ops)

    ok = all(r["status"] == "passed" for r in results)
    print(json.dumps({"schema_version": 1, "vendor": vendor, "ok": ok, "checks": results}, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
