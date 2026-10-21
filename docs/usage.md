# Usage

## Backend (GPU host, NixOS)

The backend is ComfyUI 0.37 with its native TRELLIS.2 nodes, running on official prebuilt
PyTorch wheels; torch is never compiled. In canix it is configured as `canix.trellisWorkbench`
(`root/modules/services/trellis_workbench.nix`). Elsewhere, use this flake's packages:
- `packages.x86_64-linux.comfyui-rocm` for AMD;
- `packages.x86_64-linux.comfyui-xpu` for Intel Arc.

**Upgrading torch.** Wheel versions and hashes are pinned with nix-cache-pin wheel-pins:

```sh
nix run .#cache-pin-wheel-pins -- --check-current   # pinned / allowed / newest per package
# edit the constraints in flake.nix (cache-pin.wheel-pins.{rocm,xpu}.packages), then:
nix run .#cache-pin-wheel-pins                      # rewrite nix/wheels/{rocm,xpu}.json
nix flake check                                     # coverage checks + tests
```

**One-time, online model bootstrap.** The fetch units are manual and pinned: repository, revision,
size and sha256.

```sh
sudo systemctl start model-fetch-trellis-birefnet model-fetch-trellis2
```

Everything after this runs offline.

**GPU self-test, using the service's own torch:**

```sh
sudo -u comfyui trellis-workbench-runtime-doctor
```

It reports device nodes, the ROCm/XPU torch build, the device name and architecture, free VRAM, a
matmul, and the TRELLIS sparse primitives (`index_add`, SDPA) run on the GPU.

**Exposure.** ComfyUI listens on `127.0.0.1:8188`. wg-home peers use `http://<host wg-home IP>:8190`:
- atlas: `10.123.0.5:8190`, always on;
- murph: `10.123.0.6:8190`, starts on the first request and stops after 20 idle minutes. The first
  request therefore waits for startup.

The ComfyUI browser UI is available at the same URL. To run the shipped workflow there, load
`src/trellis_workbench/workflows/trellis2.api.json`; the frontend accepts API-format files.

## Client CLI

Configuration lives in `~/.config/trellis-workbench/config.toml`. The Home Manager module
`programs.trellis-workbench` writes it.

```sh
trellis-workbench doctor                    # config, backend GPU, node schema, models, all workflow combinations
trellis-workbench workflows validate        # every stage/mode/profile against the live /object_info
trellis-workbench workflows object-info out.json   # record the backend schema

trellis-workbench submit --mode accessory --image helmet.png              # full textured GLB
trellis-workbench submit --mode garment --image coat.png --stage shape    # geometry only
trellis-workbench submit --mode human --image ref.png --mask ref_mask.png --profile low-memory \
    --reference-rights "own photo"
trellis-workbench status                    # recent jobs
trellis-workbench status <job> --watch      # follow / reconcile one job
trellis-workbench cancel <job>              # dequeue, or interrupt only if it is the running prompt
trellis-workbench bundle list
trellis-workbench bundle inspect <bundle dir>   # verify checksums, print the manifest
trellis-workbench bench --mode accessory --image ref.png --runs 3 --out bench.jsonl
```

**Exit codes.** `submit` returns 3 when the backend is *deferred* (not enough free VRAM), and 1 on
failure.

**`--profile`**

| Profile | Diffusion model | Upsample | Texture | Face budget |
|---|---|---|---|---|
| `standard` | bf16 | 1024 | 2048 | 200k |
| `low-memory` | int8_convrot | 1024 | 1024 | 100k |
| `high` | bf16 | 1536 | 4096 | 500k |

**`--mode`**

| Mode | Category and mesh path |
|---|---|
| `accessory` | Remeshed |
| `garment` | Skips the volumetric remesh to keep thin cloth single-layer |
| `human` | Remeshed |
| `animal` | Remeshed |

**Preprocessing**
- An explicit `--mask` wins. A white region is the subject (red channel).
- Otherwise an RGBA reference keeps its own alpha.
- Otherwise BiRefNet removes the background.
- The processed crop is stored in the bundle as `input/processed.png`.

**Bundles** are written to `<asset_root>/<asset_id>/rNNNN/`:
- `manifest.json`;
- `input/` (reference, processed crop, mask);
- `source/high_detail.glb`;
- `working/model.glb`;
- `textures/`;
- `reports/mesh.json`;
- `logs/job.jsonl`.

A revision is never overwritten; a new generation for the same `--asset-id` becomes the next revision.

## Blender add-on

1. In Preferences → Add-ons, enable **TRELLIS Workbench** once. The Home Manager module exposes it
   as a system extension; it lives in the *System* repository.
2. Open the sidebar (N) → **TRELLIS**.

**Generate.** Pick a reference (and optionally a mask), a mode, a quality and a backend, then press
*Generate*. Progress comes from the helper process; use *Refresh* to list jobs and bundles.

**Import.** Click a bundle (or choose a folder) to import it into a new collection
`TW <name> <id> rNNNN`. Nothing existing is replaced. Use *Set real-world height*: generated assets
have no trustworthy scale.

**Rigid accessory**
1. Press *Add landmark pairs*, then place `TW_A1..3` on the accessory and `TW_T1..3` on the target.
2. *Align by landmarks*.
3. Select the target, then the accessory, set *Bone* (or leave it empty for object parenting), and
   *Attach*.

**Garment from the body (preferred for animation)**
1. In Edit Mode, select the body faces the garment should cover.
2. *Garment from selected body faces*. Adjust the *Clearance* (Displace) and *Thickness* (Solidify)
   modifiers.

**Generated garment**
1. Import, set its height, align it with landmarks.
2. Select the body, then the garment, and *Fit generated garment*. This writes a rest-pose fitted
   copy, and loose regions beyond *Fit distance* stay untouched.
3. *Transfer weights from body*, then *Check penetration (poses)*.

**Status label.** `static` and `retopology-needed` come from what was actually done. Only an artist
sets `tw_deformation_validated` after checking deformation.
