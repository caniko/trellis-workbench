# nixpkgs' ComfyUI with its torch stack swapped for the Intel XPU wheels. Applied as a
# python-package extension so ComfyUI's own `torch.override` still composes on top.
final: prev: {
  pythonPackagesExtensions =
    prev.pythonPackagesExtensions
    ++ [
      (pyFinal: _pyPrev: {
        torch = pyFinal.callPackage ./torch-xpu-bin.nix {};
        # One derivation ships torch, torchvision, torchaudio and triton-xpu (matching wheels).
        torchvision = pyFinal.torch;
        torchaudio = pyFinal.torch;
      })
    ];
}
