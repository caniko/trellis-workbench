# Compatibility and measured results

Everything below was measured on real hardware: atlas on 2026-10-21 (its barrel rows on 2026-11-01),
the Arc A770 on 2026-11-01. Rows that were not run say so. Measurements are single runs, not averages.

## Tested stack

| Component | Version |
|---|---|
| Host | atlas: AMD Radeon RX 7900 XTX (gfx1100, 24 GiB), 62 GiB RAM, Linux 7.2.8-cachyos-lto |
| ComfyUI | 0.37.0 (nixpkgs, this flake's `comfyui-rocm`) |
| PyTorch | 2.14.1+rocm7.2, official pytorch.org wheels pinned in `nix/wheels/rocm.json` (torch `e378baf9…`, torchvision `dae5573b…`, torchaudio `239f01c8…`, triton-rocm 3.8.0 `bb278907…`) |
| Python | 3.14.7 |
| Models | Comfy-Org/TRELLIS.2 and Comfy-Org/BiRefNet at the revisions and sha256 values in `inventory.json` |
| Blender | 5.2.2 (previews and add-on import) |

ComfyUI ran on loopback with `ROCR_VISIBLE_DEVICES=0` and `HSA_OVERRIDE_GFX_VERSION=11.0.0`. The
`comfyui-rocm` wrapper sets two defaults, both needed on this card:

- `HIPBLAS_WORKSPACE_CONFIG=:65536:8`. Without it, UV unwrapping fails with
  `HIPBLAS_STATUS_ALLOC_FAILED` in rocSOLVER's batched getrf.
- `--reserve-vram 3`. Without it, a standard-profile shoe (seed 7) exhausted VRAM during texture
  decoding, and ROCm reported a GPU memory access fault that killed ComfyUI. The client marked
  the job `lost` and submitted nothing twice. With the reserve, the same reference (seed 8) completed.

## Generation runs (atlas, full stage unless noted)

"Peak VRAM" is device-wide usage sampled once per second, including the desktop. ComfyUI caches
and offloads models opportunistically, so these peaks are upper bounds, not requirements. Wall time
on the barrel rows runs from job creation to publish or failure.

| Reference | Mode | Profile | Seed | Result | Wall time | Peak VRAM |
|---|---|---|---|---|---|---|
| viking_wolf_rune_axe (shape stage only; first job after start) | accessory | standard | 7 | published | 60 s | 18.3 GiB |
| viking_wolf_rune_axe | accessory | standard | 7 | published | 87 s | 18.4 GiB |
| product_shoe | accessory | low-memory | 7 | published | 175 s | 18.5 GiB |
| product_shoe (no VRAM reserve) | accessory | standard | 7 | **backend crashed** (memory access fault) | | |
| product_shoe | accessory | standard | 8 | published | 215 s | 22.9 GiB |
| 3d_character_front_view | human | standard | 11 | published | 157 s | 20.2 GiB |
| funny_dog | animal | standard | 12 | published | 160 s | 21.3 GiB |
| clothing_shirt_flat | garment | standard | 13 | published | 122 s | 21.3 GiB |
| viking_wolf_rune_axe (repeat) | accessory | standard | 14 | published | 82 s | 17.7 GiB |
| viking_wolf_rune_axe, with 12 GiB held by another process | accessory | low-memory | 33 | published | 105 s | 23.5 GiB (incl. the 12 GiB) |
| barrel, with llama-swap holding about 9 GB | accessory | standard | 1406856676 | **out of memory** in shape decode, retried automatically | 326 s | not sampled |
| barrel (the automatic retry, its own job) | accessory | low-memory | 1406856676 | published | 509 s | not sampled |

Cold and warm timing differ little. The client calls `/free` after every job, so every job
reloads the models, from the page cache once they have been read once. The first full axe took
87 s and the repeat 82 s.

## Generation runs (murph, Arc A770, full stage)

`comfyui-xpu` on murph: 15.5 GiB RAM, Linux 7.2.11-cachyos-lto (read after the runs), and an A770
that ComfyUI reports as 15.1 GiB. ComfyUI 0.37.0, PyTorch 2.14.1+xpu, Python 3.14.7. Only
`low-memory` accessory jobs have run here, all from one reference. `standard` and `high` have not
run, and peak VRAM was not sampled. Wall time runs from job creation to publish or failure.

| Reference | Backend | Mode | Profile | Seed | Result | Wall time |
|---|---|---|---|---|---|---|
| barrel | production `comfyui.service`, before the OpenCL ICD pin | accessory | low-memory | 1123286515 | **failed** in conditioning (`could not create a primitive`), fixed by the pin | 8 s |
| barrel | test ComfyUI | accessory | low-memory | 1010229816 | **backend aborted** in remesh | |
| barrel | test ComfyUI, scatter-tracing hook loaded | accessory | low-memory | 1606065079 | published | 1045 s |
| barrel | production `comfyui.service` | accessory | low-memory | 650542645 | published | 788 s |

The abort was `ScatterGatherKernels.cpp:233 Assertion idx_dim >= 0 && idx_dim < index_size_` in
`remesh_narrow_band_dc`. The cause is unknown. It did not recur in the two later runs, and a
standalone remesh of a sphere at resolutions 128 to 768 did not reproduce it.

## Mesh statistics (exported `model.glb`, current validator)

| Asset | Vertices | Faces | Parts | Open boundary loops | Non-manifold edges | Flipped UV faces |
|---|---|---|---|---|---|---|
| axe (shape-only `high_detail.glb`) | 2,467,202 | 5,018,126 | 12 | 31,987 | 173,614 | no UVs |
| axe | 143,840 | 198,828 | 23 | 27 | 690 | 49,547 |
| axe (repeat) | 146,895 | 198,892 | 56 | 2 | 513 | 54,290 |
| shoe, low-memory | 75,815 | 93,804 | 84 | 27 | 2,180 | 9,386 |
| shoe, standard | 152,438 | 196,616 | 94 | 33 | 1,700 | 26,585 |
| human | 156,178 | 195,457 | 107 | 45 | 1,925 | 39,260 |
| dog | 167,896 | 194,949 | 94 | 29 | 1,288 | 24,569 |
| shirt (garment) | 198,046 | 187,926 | 14 | 13,118 | 3,719 | 17,220 |
| axe under VRAM pressure, low-memory | 72,834 | 97,247 | 43 | 4 | 393 | 22,256 |
| barrel, atlas retry, low-memory | 67,448 | 100,000 | 15 | 1 | 0 | 26,495 |
| barrel, A770 test ComfyUI | 66,050 | 99,996 | 11 | 0 | 0 | 47,923 |
| barrel, A770 production | 68,024 | 99,998 | 8 | 0 | 0 | 28,956 |

All bundles validate as `warning`, never `error`, and face counts stay within the profile
budgets.

Visual check: Cycles previews were rendered through the add-on's import path
(`blender_tools/render_preview.py`).

- **Axe, human and dog** match their references, with textures mapped correctly.
- **Shirt** came out as a flat, speckled shell, because the flat-lay reference gives a flat-lay
  mesh. The garment path skips the watertight remesh, which explains the 13k open loops. It is
  not wearable as-is. Use the body-derived garment path, or a worn/mannequin reference.
- **Barrel (A770, production)** matches its reference. This one was imported with Blender's glTF
  importer, not through the add-on.
- **Flipped UV faces:** TRELLIS's unwrapper mirrors whole charts, and textures still render
  correctly, so this finding is informational.

## Job control

| Test | Result |
|---|---|
| Runtime doctor and `trellis-workbench doctor` against the live `/object_info` | pass; all 24 stage/mode/profile combinations validate |
| Cancel a queued job | `dequeued`. Only that prompt was removed; the running one continued. |
| Cancel the running job | `interrupted` within 3 s; the backend stayed up and the queue was empty |
| Duplicate-free reconciliation after a backend crash or restart | jobs are marked `lost`; nothing is resubmitted |
| VRAM pressure: another process holding 12 GiB, standard profile | deferred before submission (`10.7 GiB free, needs 14.0`), exit code 3, nothing queued |
| VRAM held only by ComfyUI's own cached models | freed with `/free` while idle, then submitted (the first live run exposed this; fixed and unit-tested) |
| Same pressure, low-memory profile | published (row above) |
| OOM → low-memory retry | Reproduced on atlas on 2026-11-01: `torch.OutOfMemoryError` in shape decode, retried once at low-memory, published. VRAM exhaustion can also surface as a fatal memory access fault (the shoe run above), which kills ComfyUI and is not retried. |

## Profile thresholds

`min_free_vram_gib` is checked against ComfyUI's reported free VRAM before submission.

- **low-memory (8 GiB):** completed with 10.7 GiB available, and again in the barrel retry under
  llama-swap pressure.
- **standard (14 GiB):** completed with about 21 GiB available. With about 14.8 GiB free, a job
  passed the check and then ran out of memory in shape decode, so 14 GiB is too low when other
  processes hold VRAM.
- **high:** has not been run.
