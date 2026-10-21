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
