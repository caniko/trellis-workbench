# Pinned Comfy-Org repacks (HF revision + sha256 + byte size per file).
#
# The DINOv3 image encoder stays under Meta's DINOv3 License even though the
# repack repo is tagged MIT, so it is kept separate from the base TRELLIS.2
# weights and included only when the operator explicitly accepts the license.
rec {
  dinov3 = {
    license = "Meta DINOv3 License (non-OSI)";
    file = {
      name = "clip_vision/dino_v3_vit_l.safetensors";
      sizeBytes = 1212559776;
      sha256 = "5cb785e458de7c460579082418af81f5c62380c181599344bdc60898c63468ee";
    };
  };

  trellis2 = {
    description = "TRELLIS.2 ComfyUI repack (Comfy-Org/TRELLIS.2)";
    repo = "Comfy-Org/TRELLIS.2";
    rev = "430a9d09b2416687018c8fe8edced2ad4858a439";
    manifestName = ".ready-trellis2.json";
    baseFiles = [
      {
        name = "diffusion_models/trellis_2_bf16.safetensors";
        sizeBytes = 10338297616;
        sha256 = "bf8ebe29f2d73722ca8a17b7aa65ef07650eb26d04799ae600482058f96921b5";
      }
      {
        name = "diffusion_models/trellis_2_int8_convrot.safetensors";
        sizeBytes = 5253048192;
        sha256 = "d01952ad137213f6a868f86b6b877026276f84af5eec23069217475a0bad3a31";
      }
      {
        name = "vae/trellis_2_shape_vae_bf16.safetensors";
        sizeBytes = 1095844024;
        sha256 = "de0cb4949a76c59ee5c091a995a69bcc8c51d5aeda939f0c641a50d2a72341f4";
      }
      {
        name = "vae/trellis_2_texture_vae_bf16.safetensors";
        sizeBytes = 948461364;
        sha256 = "714e5ebf094a610e12a8e3b5175c18a62f37f6ea4218acb6073644456b73ab0e";
      }
    ];
    dinov3File = dinov3.file;
    filesFor = {acceptDinov3License ? false}:
      (
        if acceptDinov3License
        then [dinov3.file]
        else []
      )
      ++ trellis2.baseFiles;
  };

  birefnet = {
    description = "BiRefNet background removal (Comfy-Org/BiRefNet)";
    repo = "Comfy-Org/BiRefNet";
    rev = "25511f8787e51912e1480706b4e47b8f467fbf72";
    manifestName = ".ready-birefnet.json";
    files = [
      {
        name = "background_removal/birefnet.safetensors";
        sizeBytes = 444473596;
        sha256 = "9ab37426bf4de0567af6b5d21b16151357149139362e6e8992021b8ce356a154";
      }
    ];
  };

  # Convenience helper returning job specifications suitable for
  # `services.modelFetch.jobs` or any custom model fetcher.
  jobsFor = {
    modelsDir,
    acceptDinov3License ? false,
  }: {
    trellis2 = {
      inherit (trellis2) description repo rev manifestName;
      files = trellis2.filesFor {inherit acceptDinov3License;};
      kind = "comfyui-model-file";
      destDir = modelsDir;
    };
    trellis-birefnet = {
      inherit (birefnet) description repo rev files manifestName;
      kind = "comfyui-model-file";
      destDir = modelsDir;
    };
  };
}
