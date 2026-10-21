# Architecture

## Boundaries

```
reference image (+ optional mask)
  → trellis-workbench CLI / Blender add-on helper   (client host, plain Python, no torch)
      HTTP + /ws, documented ComfyUI routes only
  → ComfyUI 0.37.0 native TRELLIS.2 nodes          (GPU host: atlas ROCm, or murph XPU on demand)
  → artifacts fetched through /view, validated, published as an immutable bundle
  → Blender add-on imports a verified bundle into the open project; fitting/attachment operators
```

| Process | Owns | Never does |
|---|---|---|
| ComfyUI (systemd `comfyui.service`, loopback) | generation queue, model loading, GPU | write into user asset roots |
| `trellis-workbench` CLI / `serve-addon` helper | journal, submission, reconciliation, cancellation, bundle staging and publication | load torch or models |
| Blender add-on | main-thread scene changes via operators/timers | network, inference, background threads touching `bpy` |

Messages across process boundaries are versioned JSON (`schema.py`), never pickled objects.
The SQLite journal records jobs (asset id, prompt id, request, events); ComfyUI remains the only queue.

## Decision 1 — one generation implementation: ComfyUI's native TRELLIS.2 nodes

ComfyUI merged native TRELLIS.2 support on 2026-08-22 (PR #14718), so nixpkgs' `comfyui` 0.37.0
already contains it (`comfy/ldm/trellis2`, `comfy_extras/nodes_trellis2.py`, `nodes_mesh_postprocess.py`,
`nodes_save_3d.py`). It is implemented in plain PyTorch/SciPy. That includes the sparse convolution
(`flexgemm.py`, a `torch.searchsorted` hashmap), the attention, the voxel-to-mesh step, UV unwrapping
and the texture/normal/AO bakers (UV-space point-in-triangle rasterization and a torch BVH).

Consequences:
- **No native extensions to build.** There are no HIP or SYCL ports of cumesh, flex_gemm, o_voxel,
  nvdiffrast or nvdiffrec.
- **No OpenGL/EGL dependency.** Baking is pure torch, so there is no headless-GL failure mode and no
  silent CPU rasterizer fallback.
- **One code path for AMD and Intel.** Only the torch build differs between vendors.

**Rejected:**
- **visualbruno/ComfyUI-Trellis2, the Aero-Ex GGUF fork, egore's ROCm patches and toastmanAu's guide.**
  Each needs six HIP extension builds (cumesh, flex_gemm, o_voxel, nvdiffrast, nvdiffrec_render,
  custom_rasterizer) pinned to one ROCm/PyTorch pair, has no Intel path, and pulls in nvdiffrast and
  nvdiffrec. Those two are under the NVIDIA Source Code License, which is non-commercial.
- **ATLAS-0321/trellis2-amd.** Kept as a reference only; installing it alongside would be a second,
  conflicting stack.
- **A "Kijai TRELLIS.2 wrapper".** Kijai authored the native PR. No separate wrapper was assumed or used.
- **Pixal3D** (shipped in the same template). It needs MoGe and a separate model, and it is not part of
  this brief.

## Decision 2 — Nix-native runtime on official prebuilt torch, never compiled

nixpkgs' `comfyui` (0.37.0) is used as-is. Only its torch stack is replaced, through
`pythonPackagesExtensions`, so nixpkgs' own `torch.override` still composes.

**The replacement torch** is the official pytorch.org wheel set for each vendor:

| Variant | Host | Wheels |
|---|---|---|
| `rocm` | atlas, 7900 XTX | `torch 2.14.1+rocm7.2`, `torchvision 0.29.1+rocm7.2`, `torchaudio 2.11.0+rocm7.2`, `triton-rocm 3.8.0` |
| `xpu` | murph, Arc A770 | `torch 2.14.1+xpu`, `torchvision 0.29.1+xpu`, `torchaudio 2.11.0+xpu`, `triton-xpu 3.8.0`, plus the Intel oneAPI runtime wheels |

**Packaging**
- `nix/torch-bin.nix` installs the wheels and patches their ELF files with `autoPatchelfHook`, the
  same approach as nixpkgs' CUDA `torch-bin`. Nothing is compiled.
- The ROCm wheels bundle their own HIP/ROCm user space and use the host's `amdgpu` kernel driver.
- The XPU wheels use the host's intel-compute-runtime and Level Zero through `hardware.graphics`.

**Pinning**
- Wheel versions, files and sha256 hashes are pinned by **nix-cache-pin wheel-pins** in
  `nix/wheels/{rocm,xpu}.json`, generated from the `cache-pin.wheel-pins` declarations in
  `flake.nix`.
- Pure-Python dependencies come from nixpkgs (`providedExternally`).
- The `cache-pin-wheel-pins-<name>-coverage` flake checks fail when a sidecar drifts from its
  declaration.

**Upgrading**
1. `nix run .#cache-pin-wheel-pins -- --check-current` shows newer versions, including any blocked
   by a constraint.
2. Edit the constraints, then run `nix run .#cache-pin-wheel-pins` and `nix flake check`.

**Rejected:**
- **Compiling `pkgsRocm` torch from source.** It is not cached at any revision that ships
  ComfyUI ≥ 0.37, it takes hours, and it does not fit the nix-daemon's 16 GiB build cap. Compiling
  torch is not allowed here.
- **A pip/Ubuntu OCI image.** It means a second userspace and duplicates nixpkgs.
- **Building XPU torch with `intel-llvm`.**
- **A hand-maintained wheel list.** It has no upgrade path or drift check.

## Decision 3 — exposure to wg-home, on demand where useful

- ComfyUI listens on `127.0.0.1:8188` only. A systemd socket on the host's wg-home address
  (`<wgHomeIp>:8190`, `FreeBind=true`) forwards to it through `systemd-socket-proxyd`.
- The firewall opens 8190 on the `wg-home` interface only.
- On murph (`onDemand`), the first connection starts ComfyUI. The proxy exits after the idle timeout
  and `StopWhenUnneeded` then stops ComfyUI, which releases all VRAM on a desktop that is also used
  for gaming.
- ComfyUI has no authentication, so the trust boundary is wg-home membership.

**Rejected:**
- **A thething Caddy `*.vpn` site.** It would add a relay hop per backend host for no gain over direct
  wg-home access.
- **An API gateway, Redis, Kubernetes or a separate web frontend.** No need was demonstrated.

## Decision 4 — workflows addressed by name, validated against the live schema

- `trellis2.api.json` is the TRELLIS.2 branch of the official
  `3d_pixal3d_trellis2_image_to_model` template (workflow-templates 0.11.66, matching ComfyUI 0.37.0),
  rewritten in API format with titled nodes.
- `trellis2.presets.toml` binds *roles* (`seed`, `face_budget`, `mask_source`, ...) to
  `{node title, input name}`. Link bindings rewire a source node by title.
- Stages (`shape` and `full`) select output nodes, and the graph is pruned to what those outputs need.
- Every submission and `doctor` validates node types, required and unknown inputs, combo choices
  (which also proves the model files are provisioned) and numeric bounds against `/object_info`.
  A stale schema fails before anything is queued.

Deliberate changes from the template:
- `pad_factor` is 1.0, as the TRELLIS.2 conditioning node itself specifies. The template's 1.1 is
  Pixal3D's value.
- `drop_small_components` is 0, so separate eyes, buttons and claws survive.
- **Garments skip the UDF remesh** (`mesh_source = "TW Decode Shape"`). Unsigned-distance remeshing
  would wrap thin cloth into a two-layer shell.

The browser and the CLI run the same API JSON. ComfyUI's frontend loads API-format workflows directly.

## Decision 5 — jobs

**Submission**
- The client chooses the `prompt_id` (a UUID ComfyUI accepts in `POST /prompt`) and writes it to the
  journal *before* submitting.
- A lost response or a dropped websocket is therefore always reconciled through `/history/{id}` and
  `/queue`, never by resubmitting.
- An identical request (same backend, inputs, settings and seed) that is still in flight reuses the
  journal entry.

**Completion**
- Completion means `execution_success` and a history entry. A node `executed` event is not completion.

**Cancellation**
- A pending prompt is removed from the queue with `POST /queue {"delete": [id]}`.
- A running prompt is interrupted with `POST /interrupt {"prompt_id": id}`, which ComfyUI 0.37
  applies only when that prompt is the running one.
- The global interrupt is never used.

**Memory**
- Before submitting, `/system_stats` must report at least the profile's `min_free_vram_gib` free.
  Otherwise the job is *deferred* and nothing is submitted. Other applications are never evicted.
- On out-of-memory, the job retries once with the explicit `low-memory` profile (int8_convrot,
  1024/1024 textures, a smaller face budget). That retry is recorded. A second OOM fails.
- After a job, `/free` unloads models, but only when the queue is idle.

## Decision 6 — bundles

- A bundle is staged in `<root>/.staging/<uuid>`. Every file is checksummed and the GLB is validated.
- It is then claimed atomically with `renameat2(RENAME_NOREPLACE)` as `<root>/<asset_id>/rNNNN`.
- An existing revision can never be overwritten, and a half-written bundle is never visible.
- `verify()` rejects missing, changed or unlisted files.
- Artifact references from ComfyUI history are path-checked: a plain filename, a relative subfolder,
  and type `output`/`temp` only.

## Decision 7 — Blender

**Import**
- Imports go into a new collection named with the asset id and revision. The asset id is stored
  separately from display names.
- Selection, the active object, mode and the active collection are restored afterwards.
- Every datablock created during a failed import is removed again.
- The glTF importer's +Y→+Z conversion is the only axis conversion, verified with an asymmetric
  fixture.
- Updates create new candidate collections. Artist-edited objects are never replaced.

**Rigid accessories**
- Alignment uses a Umeyama landmark fit, then the accessory is parented to an object or bone with its
  world matrix preserved.
- No weights and no Armature modifier are added.

**Garment path B (preferred)**
- Selected body faces are duplicated into a separate object. Its deform weights are exact because
  the topology is identical.
- Clearance (Displace) and thickness (Solidify) are added as editable modifiers, and an Armature
  modifier points at the existing rig.

**Garment path A (generated garment)**
- The shrinkwrap is masked by rest-pose proximity, so loose hems stay where they are. It is evaluated
  with `pose_position = 'REST'` and baked into a new copy, so there is no live shrinkwrap stacked on
  an Armature modifier.
- Weights are then transferred from the deform-bone groups with a DataTransfer (`POLYINTERP_NEAREST`,
  max distance), normalized, and reported for unweighted and cross-side vertices.

**Diagnostics**
- Penetration is counted advisorily across temporary inspection poses. The original pose and action
  are restored exactly.

## Not built (YAGNI, documented)

- **Text-to-image.** It is optional per the brief, and comes only after image-to-3D is proven on
  hardware.
- **A custom ComfyUI metadata node.** `/system_stats` and `/history` already give versions, device
  and the exact prompt.
- **Automatic retopology, quad remeshing and universal rigging.**
- **Multi-view.** The native TRELLIS.2 nodes are single-view, and unrelated views are not geometric
  evidence.
