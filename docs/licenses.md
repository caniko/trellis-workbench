# Licenses and provenance

Machine-readable inventory: [`inventory.json`](inventory.json).

This is an engineering audit, not legal advice. Software licenses, model licenses and the rights to
reference images are tracked separately. A wrapper's own license never replaces the licenses of what
it loads.

## This repository

| Part | License |
|---|---|
| Bridge library and CLI (`src/`), Nix files, docs | MIT OR Apache-2.0 (`LICENSE-MIT`, `LICENSE-APACHE`) |
| Blender add-on (`blender_addon/trellis_workbench/`) | GPL-3.0-or-later (`blender_addon/trellis_workbench/LICENSE`). It imports `bpy`, so it is distributed under Blender's GPL terms. |

No third-party source code is copied into this repository.
- `doctor_runtime.py` runs inside ComfyUI's Python environment but imports only torch, not ComfyUI
  (GPL-3.0) modules.
- The workflow JSON is our own API-format rewrite of the TRELLIS.2 branch of an official
  Comfy-Org template.

## Runtime software (not redistributed by this repository)

| Component | Version / pin | License | Notes |
|---|---|---|---|
| ComfyUI | 0.37.0 (nixpkgs) | GPL-3.0-only | Used as a separate network service. It contains the native TRELLIS.2 reimplementation. |
| TRELLIS.2 reference code | microsoft/TRELLIS.2 | MIT | The architecture behind the native nodes. |
| PyTorch ROCm wheels | torch 2.14.1+rocm7.2, torchvision 0.29.1+rocm7.2, torchaudio 2.11.0+rocm7.2, triton-rocm 3.8.0 (sha256 pinned in `nix/wheels/rocm.json`) | BSD-3-Clause; MIT (triton) | Official pytorch.org binaries. |
| ROCm 7.2 user space bundled in the torch ROCm wheel | as shipped by pytorch.org (HIP runtime, rocBLAS, hipBLASLt, MIOpen, …) | MIT / NCSA / Apache-2.0 (per component) | Redistributed by PyTorch inside the wheel. The host's `amdgpu` kernel driver is used. |
| PyTorch XPU wheels | torch 2.14.1+xpu, torchvision 0.29.1+xpu, torchaudio 2.11.0+xpu, triton-xpu 3.8.0 (sha256 pinned in `nix/wheels/xpu.json`) | BSD-3-Clause; MIT (triton) | Official pytorch.org binaries. |
| Intel oneAPI runtime wheels | 2026.1.0: sycl-rt, cmplr-lib-rt/ur, opencl-rt, openmp, mkl, onemkl-sycl-*, tbb, umf, tcmlib, pti, dpcpp-cpp-rt, impi-rt, oneccl | Intel Simplified Software License (ISSL) | **Unfree but redistributable.** Allowed by a narrow `allowUnfreePredicate` (only `torch-xpu-bin`). |
| intel-compute-runtime, Level Zero | host `hardware.graphics` (murph) | MIT | User-mode GPU driver, not bundled. |
| Blender | 5.2 (nixpkgs) | GPL-2.0-or-later (GPL-3.0 as a whole) | The client application. |
| trimesh, numpy, scipy, pydantic, websockets, Pillow | nixpkgs | MIT, BSD-3, BSD-3, MIT, BSD-3, MIT-CMU | Client dependencies. |
| aiohttp, pytest | nixpkgs | Apache-2.0, MIT | Tests only. |

Components rejected partly for licensing:
- nvdiffrast and nvdiffrec (NVIDIA Source Code License, non-commercial).
- The HIP ports of those two, because a port does not remove the original restriction.

## Model weights (downloaded only by the manual `model-fetch-trellis*` units)

| File | Source (pinned revision) | License | Conditions |
|---|---|---|---|
| `diffusion_models/trellis_2_bf16.safetensors` | Comfy-Org/TRELLIS.2 @430a9d0 (repack of microsoft/TRELLIS.2-4B) | MIT | Keep the MIT notice. |
| `diffusion_models/trellis_2_int8_convrot.safetensors` | same | MIT | A third-party (Comfy-Org) quantization. Quality must be validated before any default use, so only the explicit `low-memory` profile uses it. |
| `vae/trellis_2_{shape,texture}_vae_bf16.safetensors` | same | MIT | |
| `clip_vision/dino_v3_vit_l.safetensors` | same repo, but DINOv3 ViT-L weights by Meta | **DINOv3 License** (`LicenseRef-DINOv3`, not OSI) | Commercial use is permitted. Redistribution requires shipping the license and prominently displaying "Built with DINOv3". Military/warfare, nuclear, espionage and weapons uses are prohibited, and trade controls (ITAR) apply. Meta may terminate on breach. Upstream (`facebook/dinov3-*`) is gated. The repack is not gated, but the license still applies. |
| `background_removal/birefnet.safetensors` | Comfy-Org/BiRefNet @25511f8 (ZhengPeng7/BiRefNet) | MIT | |

**Profiles:**
- **`mixed`** is what atlas and murph run. The operator accepted the DINOv3 License for this host
  (canix `acceptedLicenses = ["dinov3"]`). Generation is available.
- **`strict-foss`** (the client default) cannot generate: TRELLIS.2 conditioning requires DINOv3, and
  substituting another encoder would break the model. Import, fitting and attachment still work on
  existing GLBs, and the CLI says so instead of degrading silently.

## Outputs and references

- Each bundle manifest records:
  - every model file the workflow loaded, with its license (`licensing.components`);
  - `reference_rights` as supplied by the user (default `unspecified`). It is not verified.
- No ownership, originality or permission is claimed for generated assets.
- Downloadable weights do not imply commercial clearance for any particular output or use.
- If you ship a product or website built with these outputs, the DINOv3 attribution condition above
  applies to the DINO Materials. Check whether your use redistributes them.
