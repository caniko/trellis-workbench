# trellis-workbench

A local image-to-3D workflow, from a reference image to an asset in an existing Blender project:

1. Choose a reference.
2. Generate with TRELLIS.2 in ComfyUI.
3. Inspect the result as a validated, immutable asset bundle.
4. Import it into the current scene.
5. Fit or attach it.

- **Generation** uses ComfyUI's native TRELLIS.2 nodes, which are pure PyTorch with no custom
  extensions. They run on official prebuilt PyTorch wheels, for AMD ROCm (`comfyui-rocm`) or
  Intel Arc XPU (`comfyui-xpu`), both from this flake.
- **The bridge CLI** (`trellis-workbench`) submits, follows, reconciles and cancels jobs over
  ComfyUI's documented HTTP and WebSocket routes, then publishes checksummed bundles atomically.
- **The Blender add-on** imports bundles without touching existing data. It provides landmark
  alignment and bone attachment for rigid accessories, and two garment paths: body-derived, and
  generated-then-fitted with weight transfer.

## Documentation

| Document | Contents |
|---|---|
| [docs/usage.md](docs/usage.md) | Commands and add-on workflow |
| [docs/architecture.md](docs/architecture.md) | Decisions and rejected alternatives |
| [docs/licenses.md](docs/licenses.md), [docs/inventory.json](docs/inventory.json) | Code, model and reference-image licensing |
| [docs/compatibility.md](docs/compatibility.md) | Tested versions and measured results |
| [docs/limitations.md](docs/limitations.md) | What this does not do |

## Checks

```sh
nix flake check   # unit/contract tests (fake ComfyUI), headless Blender acceptance tests, formatting
```

The checks need no models and no GPU. Real-hardware results are recorded separately in
`docs/compatibility.md`.

## Licensing

- Code: MIT OR Apache-2.0.
- Blender add-on: GPL-3.0-or-later.
- TRELLIS.2 generation needs the DINOv3 encoder, which is under Meta's DINOv3 License, so the
  default `strict-foss` profile refuses to generate. Details are in `docs/licenses.md`.
