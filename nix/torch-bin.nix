# PyTorch from the official pytorch.org wheels, pinned by nix-cache-pin wheel-pins
# (`nix/wheels/<variant>.json`). Nothing is compiled: the wheels are installed as-is and their
# ELF files are patched against Nix libraries, like nixpkgs' CUDA torch-bin.
#
# One derivation carries torch, torchvision, torchaudio, triton and (XPU) the Intel runtime
# wheels, so cross-wheel `$ORIGIN` references resolve inside a single prefix.
#
# - rocm: the wheels bundle their own HIP/ROCm user space; the host's amdgpu kernel driver
#   (/dev/kfd, render node) is used.
# - xpu: the GPU user-mode driver (intel-compute-runtime) comes from the host's
#   hardware.graphics via /run/opengl-driver/lib.
{
  lib,
  variant,
  python,
  buildPythonPackage,
  fetchurl,
  autoPatchelfHook,
  autoAddDriverRunpath,
  stdenv,
  zlib,
  numactl,
  libuuid,
  level-zero,
  ocl-icd,
  libdrm,
  elfutils,
  zstd,
  ncurses,
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
  # keep working; neither variant uses CUDA.
  cudaPackages ? null,
}: let
  pins = lib.importJSON ./wheels/${variant}.json;
  wheels = map (w:
    fetchurl {
      inherit (w) url sha256;
      name = w.filename;
    })
  pins.wheels;
  torchPin = lib.findFirst (w: w.pname == "torch") (throw "wheel pin ${variant} has no torch") pins.wheels;
  perVariant = {
    rocm = {
      buildInputs = [numactl libdrm elfutils zstd ncurses];
      ignoreMissing = [];
      license = [];
      # torch bundles the HIP runtime only as libamdhip64.so (SONAME libamdhip64.so.7);
      # torchvision's _C_stable.so asks for the SONAME, so expose it under that name too.
      preFixup = ''
        lib=$out/${python.sitePackages}/torch/lib
        test -e $lib/libamdhip64.so.7 || ln -s libamdhip64.so $lib/libamdhip64.so.7
        # torch and triton-rocm bundle old copies of libnuma whose constructors exit when sysfs
        # is unavailable (e.g. the Nix sandbox); resolve libnuma from nixpkgs' numactl instead.
        find $out -name 'libnuma.so*' -delete
      '';
    };
    xpu = {
      buildInputs = [level-zero ocl-icd numactl libuuid];
      # libfabric (inside oneCCL/Intel MPI) dlopens these providers only on cluster
      # interconnects (InfiniBand verbs, AWS EFA, Omni-Path PSM2, UCX).
      ignoreMissing = ["libibverbs.so.1" "librdmacm.so.1" "libefa.so.1" "libpsm2.so.2" "libucp.so.0"];
      license = [lib.licenses.issl]; # Intel oneAPI runtime wheels
      preFixup = "";
    };
  };
  v = perVariant.${variant};
in
  buildPythonPackage {
    pname = "torch";
    inherit (torchPin) version;
    format = "wheel";

    srcs = wheels;
    dontUnpack = true;
    preInstall = ''
      mkdir -p dist
      # Symlink, don't copy: the ROCm torch wheel alone is 6.2 GB.
      for whl in ${lib.escapeShellArgs wheels}; do
        ln -s "$whl" "dist/$(stripHash "$whl")"
      done
    '';

    nativeBuildInputs = [autoPatchelfHook autoAddDriverRunpath];
    buildInputs = [zlib stdenv.cc.cc.lib] ++ v.buildInputs;
    autoPatchelfIgnoreMissingDeps = v.ignoreMissing;
    inherit (v) preFixup;

    dependencies = [filelock fsspec jinja2 networkx numpy pillow pyelftools setuptools sympy typing-extensions];

    # Runtime wheels listed in torch's metadata are installed into this same prefix, which the
    # runtime-deps check does not consider.
    dontCheckRuntimeDeps = true;
    pythonImportsCheck = ["torch" "torchvision" "torchaudio"];

    passthru = {
      inherit variant;
      wheelPins = pins;
      xpu = variant == "xpu";
      unfreeRedistributable = variant == "xpu";
    };

    meta = {
      description = "PyTorch ${torchPin.version} (official prebuilt ${variant} wheels)";
      homepage = "https://pytorch.org/";
      license = [lib.licenses.bsd3 lib.licenses.mit] ++ v.license;
      sourceProvenance = [lib.sourceTypes.binaryNativeCode];
      platforms = ["x86_64-linux"];
    };
  }
