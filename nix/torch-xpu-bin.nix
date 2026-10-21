# PyTorch for Intel XPU (Arc/DG2 via Level Zero) from the official pytorch.org wheels.
#
# nixpkgs' torch has no XPU build, so this follows nixpkgs' torch-bin pattern: install the
# pinned wheels as-is and patch their ELF files against Nix libraries. One derivation carries
# torch, torchvision, torchaudio, triton-xpu and the Intel runtime wheels, so every
# cross-wheel `$ORIGIN` library reference resolves inside a single prefix.
#
# The GPU user-mode driver (intel-compute-runtime) is not bundled: libze_loader finds it in
# /run/opengl-driver/lib, provided by the host's hardware.graphics configuration.
{
  lib,
  buildPythonPackage,
  fetchurl,
  autoPatchelfHook,
  autoAddDriverRunpath,
  level-zero,
  ocl-icd,
  numactl,
  libuuid,
  zlib,
  stdenv,
  filelock,
  fsspec,
  jinja2,
  networkx,
  numpy,
  pillow,
  pyelftools,
  setuptools,
  sympy,
  typing-extensions,
  # Accepted so consumers that `torch.override { cudaPackages = ...; }` (e.g. nixpkgs comfyui)
  # keep working; XPU never uses it.
  cudaPackages ? null,
}: let
  wheels = map fetchurl (import ./xpu-wheels.nix);
in
  buildPythonPackage {
    pname = "torch";
    version = "2.14.1+xpu";
    format = "wheel";

    # Several wheels instead of one: place them all in dist/ for pypaInstallHook.
    srcs = wheels;
    dontUnpack = true;
    preInstall = ''
      mkdir -p dist
      for whl in ${lib.escapeShellArgs wheels}; do
        cp "$whl" "dist/$(stripHash "$whl")"
      done
    '';

    nativeBuildInputs = [
      autoPatchelfHook
      autoAddDriverRunpath
    ];

    buildInputs = [
      level-zero
      ocl-icd
      numactl
      libuuid
      zlib
      stdenv.cc.cc.lib
    ];

    dependencies = [
      filelock
      fsspec
      jinja2
      networkx
      numpy
      pillow
      pyelftools
      setuptools
      sympy
      typing-extensions
    ];

    # libfabric (inside oneCCL/Intel MPI) dlopens these hardware providers only on cluster
    # interconnects (InfiniBand verbs, AWS EFA, Omni-Path PSM2, UCX); single-GPU use never loads them.
    autoPatchelfIgnoreMissingDeps = [
      "libibverbs.so.1"
      "librdmacm.so.1"
      "libefa.so.1"
      "libpsm2.so.2"
      "libucp.so.0"
    ];

    # The Intel runtime wheels are listed as torch's requirements; they are installed in this
    # same prefix, which the runtime-deps check does not consider.
    dontCheckRuntimeDeps = true;

    pythonImportsCheck = [
      "torch"
      "torchvision"
      "torchaudio"
    ];

    passthru.xpu = true;

    meta = {
      description = "PyTorch 2.14.1 with Intel XPU support (official prebuilt wheels)";
      homepage = "https://pytorch.org/";
      license = with lib.licenses; [
        bsd3 # torch, torchvision, torchaudio
        mit # triton-xpu
        issl # Intel oneAPI runtime wheels (sycl-rt, mkl, tbb, umf, pti, ...)
      ];
      sourceProvenance = [lib.sourceTypes.binaryNativeCode];
      platforms = ["x86_64-linux"];
    };
  }
