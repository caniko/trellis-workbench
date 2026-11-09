# trellis-workbench vision

`trellis-workbench` is a local image-to-3D asset workflow: choose a reference
image, generate a mesh with TRELLIS.2 inside ComfyUI, inspect it as a validated
immutable asset bundle, then import and fit it into an existing Blender project.
It has three parts: a bridge CLI that submits, follows, reconciles and cancels
ComfyUI jobs and publishes bundles; a Blender add-on that imports them without
touching existing data and offers landmark alignment, bone attachment and two
garment-fitting paths; and a Nix-native ComfyUI runtime on official ROCm or XPU
PyTorch wheels. It serves artists working on props, accessories, characters and
garments.

## Problem

Image-to-3D models produce a plausible mesh and no workflow: a directory of
outputs with no identity, no validation, no way into a scene, and a GPU stack
that only works on one vendor. The workbench exists to make generation a step
rather than a project: a job has durable state, an output is checksummed and
reviewed before it enters the asset library, and the Blender side fits the
result to the user's existing rig and landmarks instead of assuming a template.

## Principles

- **One generation implementation.** Use ComfyUI's native TRELLIS.2 nodes, which
  are plain PyTorch, instead of HIP/SYCL extension forks. One code path for AMD
  and Intel; only the torch build differs.
- **Nix-native on official wheels, never compiled.** nixpkgs ComfyUI is used
  as-is with its torch stack replaced through `pythonPackagesExtensions`;
  official pytorch.org wheels are pinned per vendor.
- **Immutability and validation before import.** Jobs are published as
  checksummed, validated bundles atomically; the bundle is the contract between
  generation and Blender.
- **Each process owns one thing.** ComfyUI owns the queue, model loading and
  GPU; the CLI owns the journal, submission, reconciliation, cancellation and
  publication; the add-on makes main-thread scene changes only. The CLI never
  loads torch, the add-on never touches the network.
- **Versioned JSON at every boundary.** Cross-process messages use versioned
  schemas, never pickled objects. The SQLite journal records jobs while ComfyUI
  remains the only queue.
- **Measure on real hardware.** `docs/compatibility.md` records per-reference
  runs with wall time and peak VRAM on the RX 7900 XTX and Arc A770, and says
  which rows were not run. Limitations are documented, not smoothed over.
- **Checks need no models and no GPU.** Unit and contract tests use a fake
  ComfyUI, plus headless Blender acceptance tests; real-hardware results are
  recorded separately.
- **Licences are tracked per artifact.** Code, add-on, models and reference
  images each have terms in `inventory.json`; the DINOv3 encoder makes the
  default `strict-foss` profile refuse to generate.

## Scope

- `comfyui-rocm` and `comfyui-xpu` packages with pinned wheels and vendor
  defaults.
- The `trellis-workbench` CLI: submit, follow, reconcile, cancel, stage and
  publish bundles; a `serve-addon` helper for the add-on.
- The Blender add-on: verified bundle import, landmark alignment, bone
  attachment for rigid accessories, body-derived and generated-then-fitted
  garment paths with weight transfer.
- Mesh validation and reporting (findings are reported, never silently
  repaired).
- Documentation: usage, architecture and rejected alternatives, licensing
  inventory, tested compatibility, and limitations.

## Non-goals

- Multi-view or text conditioning, or text-to-image generation.
- Animation topology, automatic quad remeshing, deformation retopology, blend
  shapes, or editable hair and fur.
- Cloth simulation or tailoring; fitting is an aid, and weight transfer is an
  initial binding that still needs manual painting.
- Training or fine-tuning models, or shipping model weights in the flake.
- Providing a humanoid template for quadrupeds, wings or tails.

## Direction

The recent history is the shape of the work: after the initial workbench and
pinned-wheel packaging, commits fixed job control and mesh validation on real
ROCm runs, recorded measured results for the RX 7900 XTX and then the Arc A770,
and corrected those docs after review. Two XPU follow-ups shipped the Level
Zero driver and pinned the shipped driver's OpenCL ICD, with one A770 job still
aborted in remesh for an unknown reason. CI is Simit-generated with grouped Nix
checks and an Attic prebuild on trunk.

## Place in the fleet

Canix consumes the repository as the `trellis-workbench` flake input and runs
`canix.trellisWorkbench` on atlas (ROCm, on-demand ComfyUI behind a wg-home
socket) and murph (XPU, on-demand with a 20-minute idle exit); the
`trellis-3d-asset` skill routes local GLB generation through those backends,
and the Paperclip graphics company owns the repository's work.
