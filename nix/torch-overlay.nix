# Swap nixpkgs' torch stack for the pinned prebuilt wheels of one variant ("rocm" | "xpu").
# Applied as a python-package extension so nixpkgs comfyui's own `torch.override` composes.
variant: _final: prev:
{
  pythonPackagesExtensions =
    prev.pythonPackagesExtensions
    ++ [
      (pyFinal: _pyPrev: {
        torch = pyFinal.callPackage ./torch-bin.nix {inherit variant;};
        # One derivation ships torch, torchvision, torchaudio and triton (matching wheels).
        torchvision = pyFinal.torch;
        torchaudio = pyFinal.torch;
      })
    ];
}
// prev.lib.optionalAttrs (variant == "rocm") {
  comfyui = prev.comfyui.overrideAttrs (old: {
    # - PyTorch's default hipBLAS workspace is too small for rocSOLVER's batched getrf: batched
    #   float64 solves (ComfyUI's UV unwrap) fail with HIPBLAS_STATUS_ALLOC_FAILED. 8 x 64 MiB
    #   fixes it; an explicit HIPBLAS_WORKSPACE_CONFIG from the environment still wins.
    # - Measured on a 24 GiB RX 7900 XTX: geometry-heavy TRELLIS.2 jobs exhaust VRAM in texture
    #   decoding while the diffusion model is still cached, which ROCm reports as a GPU memory
    #   access fault that kills the process. Reserving 3 GiB makes ComfyUI evict cached models
    #   first. A later --reserve-vram on the command line overrides this default.
    postFixup =
      (old.postFixup or "")
      + ''
        wrapProgram $out/bin/comfyui \
          --set-default HIPBLAS_WORKSPACE_CONFIG :65536:8 \
          --add-flags "--reserve-vram 3"
      '';
  });
}
// prev.lib.optionalAttrs (variant == "xpu") {
  comfyui = prev.comfyui.overrideAttrs (old: let
    # The host's Level Zero driver is built against the host's newer libstdc++ (GLIBCXX_3.4.35),
    # but this process loads the flake nixpkgs' older libstdc++ first, so the host driver fails to
    # load, the loader finds 0 drivers and torch.xpu.device_count() is 0. Ship the driver from
    # this nixpkgs instead; the host's libze_loader is still used, only the driver is swapped.
    env = {
      ZE_ENABLE_ALT_DRIVERS = "${prev.intel-compute-runtime}/lib/libze_intel_gpu.so.1";
      # ponytail: the driver finds libigc via this path until the nixpkgs pin has abf51db76b
      # (NixOS/nixpkgs#575155, RUNPATH to intel-graphics-compiler); drop it then.
      LD_LIBRARY_PATH = "${prev.intel-graphics-compiler}/lib";
    };
  in {
    # --set-default: an operator can still point ZE_ENABLE_ALT_DRIVERS elsewhere.
    postFixup =
      (old.postFixup or "")
      + ''
        wrapProgram $out/bin/comfyui \
          --set-default ZE_ENABLE_ALT_DRIVERS ${env.ZE_ENABLE_ALT_DRIVERS} \
          --prefix LD_LIBRARY_PATH : ${env.LD_LIBRARY_PATH}
      '';
    # Read by the runtime doctor so it sees the same driver environment as the service.
    passthru = (old.passthru or {}) // {xpuDriverEnv = env;};
  });
}
