# Client side: CLI, configuration and the Blender add-on (system extension repository).
self: {
  config,
  lib,
  pkgs,
  ...
}: let
  inherit (lib) mkEnableOption mkIf mkOption types;
  cfg = config.programs.trellis-workbench;
  system = pkgs.stdenv.hostPlatform.system;
  addon = self.packages.${system}.trellis-workbench-blender-addon;
  extensions = "${addon}/${addon.systemExtensions}";
  settings =
    {
      backends = cfg.backends;
      asset_root = cfg.assetRoot;
      allowed_roots = cfg.allowedRoots;
      license_profile = cfg.licenseProfile;
      helper = lib.getExe cfg.package;
    }
    // lib.optionalAttrs (cfg.defaultBackend != null) {default_backend = cfg.defaultBackend;};
in {
  options.programs.trellis-workbench = {
    enable = mkEnableOption "the TRELLIS.2 asset workbench client (CLI + Blender add-on)";
    package = mkOption {
      type = types.package;
      default = self.packages.${system}.trellis-workbench;
      description = "trellis-workbench CLI package.";
    };
    backends = mkOption {
      type = types.attrsOf types.str;
      default = {};
      example = {atlas = "http://10.123.0.5:8190";};
      description = "Named ComfyUI backends (http URLs).";
    };
    defaultBackend = mkOption {
      type = types.nullOr types.str;
      default = null;
      description = "Backend used when none is given.";
    };
    assetRoot = mkOption {
      type = types.str;
      default = "${config.xdg.dataHome}/trellis-workbench/assets";
      description = "Where published asset bundles are written.";
    };
    allowedRoots = mkOption {
      type = types.listOf types.str;
      default = [cfg.assetRoot];
      defaultText = lib.literalExpression "[ cfg.assetRoot ]";
      description = "Allowlist the asset root must resolve into.";
    };
    licenseProfile = mkOption {
      type = types.enum ["mixed" "strict-foss"];
      default = "strict-foss";
      description = ''
        "mixed" allows TRELLIS.2 generation, whose conditioning needs the DINOv3 encoder
        (DINOv3 License, not OSI). "strict-foss" refuses to generate.
      '';
    };
    blenderAddon = mkOption {
      type = types.bool;
      default = true;
      description = "Expose the add-on to Blender as a system extension (enable it once in Preferences).";
    };
  };

  config = mkIf cfg.enable {
    assertions = [
      {
        assertion = cfg.defaultBackend == null || cfg.backends ? ${cfg.defaultBackend};
        message = "programs.trellis-workbench.defaultBackend must name one of programs.trellis-workbench.backends.";
      }
    ];
    home.packages = [cfg.package];
    xdg.configFile."trellis-workbench/config.toml".source = (pkgs.formats.toml {}).generate "trellis-workbench.toml" settings;
    home.sessionVariables = mkIf cfg.blenderAddon {BLENDER_SYSTEM_EXTENSIONS = extensions;};
    systemd.user.sessionVariables = mkIf cfg.blenderAddon {BLENDER_SYSTEM_EXTENSIONS = extensions;};
  };
}
