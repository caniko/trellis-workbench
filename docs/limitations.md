# Limitations

**Generation**
- Generation is single-view. There is no multi-view or text conditioning. Text-to-image is not
  shipped.
- A character turntable sheet is not one object image. Crop a single view.
- Output scale is arbitrary until you set a height.
- Hair and fur are baked into the mesh and textures, not an editable groom. Faces have no blend
  shapes.
- Materials are what TRELLIS.2 bakes:
  - base color with alpha, metallic and roughness from the voxel decoder;
  - normal and AO baked from the remeshed high-poly.
  - Nothing else is fabricated.
- Quality and memory figures in `compatibility.md` are measured only where stated. Only the
  low-memory `min_free_vram_gib` threshold has been exercised under VRAM pressure; nothing has run
  on the Arc A770 yet.
- Flat-lay garment references give flat, open shells. Use a worn or mannequin view, or the
  body-derived garment path.

**Topology**
- Decoded meshes are triangulated, dense, and not animation topology. Decimation is
  quadric/midpoint collapse before UV unwrapping.
- There is no automatic quad remeshing or deformation retopology. Retopology must be followed by a
  rebake from the retained source (`source/high_detail.glb`). No voxel latent is retained.
- The UDF remesh used for accessories, humans and animals makes surfaces watertight-ish shells.
  Garments skip it to keep cuffs, hems and openings, so garment meshes can keep TRELLIS's own
  defects.
- Mesh validation reports these findings and never repairs them. Self-intersection is not certified.

**Fitting**
- Fitting is an aid, not tailoring. The generated-garment shrinkwrap only pulls vertices within
  *Fit distance* in rest pose. Loose coats and skirts are not draped.
- Weight transfer is an initial binding. Expect manual weight painting. The cross-side report relies
  on `.L/.R`-style bone names.
- Penetration counts are advisory: nearest-face normal tests at a few inspection poses.
- Rigging is limited to the user's existing rig and landmarks. No humanoid template is assumed for
  quadrupeds, wings or tails.
- Cloth simulation is not set up. Do it yourself if you need it.

**Operations**
- ComfyUI has no authentication. Anyone on wg-home can submit jobs, read outputs and see the queue.
- `/free` unloads all models in ComfyUI after a job, but only when its queue is idle. It also affects
  other users' cached models.
- The VRAM preflight sees the backend's view (`/system_stats`). Other GPU users (llama-swap,
  Blender, games) are never stopped. Jobs are deferred instead.
- A job lost to a backend restart before it ran is marked `lost`. In-memory sampler state is never
  recovered. Rerun deliberately.
