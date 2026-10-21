{
  description = "Local TRELLIS.2 (ComfyUI) to Blender asset workbench";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
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
  };

  outputs = {
    self,
    nixpkgs,
    treefmt-nix,
    harbor-rs,
  }: let
    system = "x86_64-linux";
    inherit (nixpkgs) lib;
    pkgs = import nixpkgs {
      inherit system;
      overlays = [self.overlays.default];
      # Only the Intel oneAPI runtime wheels inside torch-xpu-bin are unfree (ISSL).
      config.allowUnfreePredicate = p: p.passthru.xpu or false;
    };
    xpuPkgs = pkgs.extend self.overlays.xpu;
    treefmt = treefmt-nix.lib.evalModule pkgs ./nix/treefmt.nix;
  in {
    overlays = {
      default = final: prev: {
        pythonPackagesExtensions =
          prev.pythonPackagesExtensions
          ++ [(pyFinal: _: {trellis-workbench = pyFinal.callPackage ./nix/package.nix {};})];
        trellis-workbench = final.python3Packages.toPythonApplication final.python3Packages.trellis-workbench;
        trellis-workbench-blender-addon = final.callPackage ./nix/blender-addon.nix {};
      };
      # Swap nixpkgs' torch for the Intel XPU wheels (nixpkgs has no XPU torch).
      xpu = import ./nix/comfyui-xpu.nix;
    };

    lib.runtimeDoctor = import ./nix/runtime-doctor.nix;

    packages.${system} = {
      default = pkgs.trellis-workbench;
      inherit (pkgs) trellis-workbench trellis-workbench-blender-addon blender;
      torch-xpu-bin = xpuPkgs.python3Packages.torch;
      comfyui-xpu = xpuPkgs.comfyui;
    };

    homeModules.default = import ./nix/hm-module.nix self;

    # Used by the simit-generated prebuild workflow on the default branch (secret ATTIC_TOKEN).
    apps.${system}.push-flake-inputs = harbor-rs.lib.mkAtticPush {
      inherit pkgs;
      flake = ".";
      paths = with self.packages.${system}; [trellis-workbench trellis-workbench-blender-addon];
      adapter = harbor-rs.lib.mkAdapter {
        attic = {
          endpoint = "https://attic.candee.baby";
          cache = "canix";
          tokenEnvVar = "ATTIC_TOKEN";
        };
      };
    };

    checks.${system} = {
      inherit (pkgs) trellis-workbench;
      formatting = treefmt.config.build.check self;
      blender = pkgs.callPackage ./nix/blender-tests.nix {src = self;};
      # XPU torch must really be the XPU build: it declares the Intel runtime and imports.
      torch-xpu-is-xpu = assert self.packages.${system}.torch-xpu-bin.passthru.xpu;
      assert lib.hasSuffix "+xpu" self.packages.${system}.torch-xpu-bin.version;
        pkgs.emptyFile;
    };

    devShells.${system}.default = pkgs.mkShell {
      packages = [
        (pkgs.python3.withPackages (ps: ps.trellis-workbench.dependencies ++ [ps.pytest ps.aiohttp]))
        pkgs.blender
        pkgs.ruff
        treefmt.config.build.wrapper
      ];
    };

    formatter.${system} = treefmt.config.build.wrapper;
  };
}
