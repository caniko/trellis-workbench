{
  description = "Local TRELLIS.2 (ComfyUI) to Blender asset workbench";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-parts = {
      url = "github:hercules-ci/flake-parts";
      inputs.nixpkgs-lib.follows = "nixpkgs";
    };
    treefmt-nix = {
      url = "github:numtide/treefmt-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    # Only for lib.mkAtticPush (CI publication to the canix Attic cache).
    harbor-rs = {
      url = "github:caniko/harbor-rs";
      inputs.nixpkgs.follows = "nixpkgs";
      inputs.treefmt-nix.follows = "treefmt-nix";
    };
    # Wheel pins: prebuilt PyTorch ROCm/XPU wheels pinned to exact files and hashes.
    nix-cache-pin = {
      url = "github:caniko/nix-cache-pin/0.3.0";
      inputs.nixpkgs.follows = "nixpkgs";
      inputs.flake-parts.follows = "flake-parts";
    };
  };

  outputs = inputs @ {
    self,
    nixpkgs,
    flake-parts,
    treefmt-nix,
    harbor-rs,
    nix-cache-pin,
  }:
    flake-parts.lib.mkFlake {inherit inputs;} {
      systems = ["x86_64-linux"];
      imports = [nix-cache-pin.flakeModules.default];

      # Official pytorch.org wheels; pure-Python dependencies come from nixpkgs.
      # Upgrade: `nix run .#cache-pin-wheel-pins -- --check-current`, then
      # `nix run .#cache-pin-wheel-pins` and `nix flake check`.
      cache-pin = let
        fromNixpkgs = ["filelock" "fsspec" "jinja2" "networkx" "numpy" "pillow" "pyelftools" "setuptools" "sympy" "typing-extensions"];
      in {
        nixpkgs = nixpkgs.legacyPackages.x86_64-linux;
        pins = {};
        wheel-pins = {
          rocm = {
            indexes = ["https://download.pytorch.org/whl/rocm7.2" "https://pypi.org/simple"];
            packages = {
              torch = "==2.14.1";
              torchvision = "==0.29.1";
              torchaudio = "==2.11.0";
            };
            localVersion = "rocm7.2";
            pythonVersion = "3.14";
            providedExternally = fromNixpkgs;
            outputFile = "nix/wheels/rocm.json";
          };
          xpu = {
            indexes = ["https://download.pytorch.org/whl/xpu" "https://pypi.org/simple"];
            packages = {
              torch = "==2.14.1";
              torchvision = "==0.29.1";
              torchaudio = "==2.11.0";
            };
            localVersion = "xpu";
            pythonVersion = "3.14";
            providedExternally = fromNixpkgs;
            outputFile = "nix/wheels/xpu.json";
          };
        };
      };

      flake = {
        overlays = {
          default = final: prev: {
            pythonPackagesExtensions =
              prev.pythonPackagesExtensions
              ++ [(pyFinal: _: {trellis-workbench = pyFinal.callPackage ./nix/package.nix {};})];
            trellis-workbench = final.python3Packages.toPythonApplication final.python3Packages.trellis-workbench;
            trellis-workbench-blender-addon = final.callPackage ./nix/blender-addon.nix {};
          };
          # Swap nixpkgs' torch for the pinned prebuilt wheels (no torch compilation).
          rocm = import ./nix/torch-overlay.nix "rocm";
          xpu = import ./nix/torch-overlay.nix "xpu";
        };
        lib.runtimeDoctor = import ./nix/runtime-doctor.nix;
        homeModules.default = import ./nix/hm-module.nix self;
      };

      perSystem = {system, ...}: let
        inherit (nixpkgs) lib;
        pkgs = import nixpkgs {
          inherit system;
          overlays = [self.overlays.default];
          # Only the Intel oneAPI runtime wheels inside the XPU torch are unfree (ISSL).
          config.allowUnfreePredicate = p: p.passthru.unfreeRedistributable or false;
        };
        rocmPkgs = pkgs.extend self.overlays.rocm;
        xpuPkgs = pkgs.extend self.overlays.xpu;
        treefmt = treefmt-nix.lib.evalModule pkgs ./nix/treefmt.nix;
      in {
        packages = {
          default = pkgs.trellis-workbench;
          inherit (pkgs) trellis-workbench trellis-workbench-blender-addon blender;
          torch-rocm-bin = rocmPkgs.python3Packages.torch;
          torch-xpu-bin = xpuPkgs.python3Packages.torch;
          comfyui-rocm = rocmPkgs.comfyui;
          comfyui-xpu = xpuPkgs.comfyui;
        };

        # Used by the simit-generated prebuild workflow on the default branch (secret ATTIC_TOKEN).
        apps.push-flake-inputs = harbor-rs.lib.mkAtticPush {
          inherit pkgs;
          flake = ".";
          paths = with pkgs; [trellis-workbench trellis-workbench-blender-addon];
          adapter = harbor-rs.lib.mkAdapter {
            attic = {
              endpoint = "https://attic.candee.baby";
              cache = "canix";
              tokenEnvVar = "ATTIC_TOKEN";
            };
          };
        };

        checks = {
          inherit (pkgs) trellis-workbench;
          formatting = treefmt.config.build.check self;
          blender = pkgs.callPackage ./nix/blender-tests.nix {src = self;};
          # The prebuilt torch packages must really be the pinned GPU builds.
          torch-wheels-are-pinned-variants = let
            rocm = rocmPkgs.python3Packages.torch;
            xpu = xpuPkgs.python3Packages.torch;
          in
            assert rocm.passthru.variant == "rocm" && lib.hasSuffix "+rocm7.2" rocm.version;
            assert xpu.passthru.variant == "xpu" && lib.hasSuffix "+xpu" xpu.version;
            assert rocmPkgs.comfyui.passthru.python.pkgs.torch.passthru.variant == "rocm";
              pkgs.emptyFile;
        };

        devShells.default = pkgs.mkShell {
          packages = [
            (pkgs.python3.withPackages (ps: ps.trellis-workbench.dependencies ++ [ps.pytest ps.aiohttp]))
            pkgs.blender
            pkgs.ruff
            treefmt.config.build.wrapper
          ];
        };

        formatter = treefmt.config.build.wrapper;
      };
    };
}
