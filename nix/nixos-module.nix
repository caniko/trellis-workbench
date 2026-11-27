# TRELLIS.2 asset-generation backend (ComfyUI native nodes).
#
# When `services.infernix.comfyui` is present in the NixOS configuration, this
# module configures it on loopback only with offline Hugging Face / Transformers
# environment variables and the TRELLIS.2 extra-model-paths YAML.
#
# Where `proxy.enable` is set, clients reach ComfyUI through a systemd socket
# bound to `proxy.listenAddress` (FreeBind, so no boot race with dynamic links)
# and `systemd-socket-proxyd`. In on-demand mode the first connection starts
# ComfyUI, and both the proxy and ComfyUI stop again after `idleTimeout`,
# releasing VRAM. ComfyUI has no authentication: restrict access via
# `proxy.listenAddress` and `proxy.firewallInterface`.
#
# Pinned model weights are exposed via `resolvedModelPins` (and `lib.modelPins`)
# and optionally wired into `services.modelFetch.jobs` when `modelFetch.enable`
# is true.
self: {
  config,
  lib,
  options,
  pkgs,
  ...
}: let
  inherit (lib) mkEnableOption mkIf mkOption types;
  cfg = config.services.trellisWorkbench;
  system = pkgs.stdenv.hostPlatform.system;
  modelPins = self.lib.modelPins;

  hasInfernixComfyui =
    (options.services ? infernix)
    && (options.services.infernix ? comfyui);

  hasInfernixGpu =
    (options.services ? infernix)
    && (options.services.infernix ? gpu)
    && (options.services.infernix.gpu ? vendor);

  hasModelFetch =
    (options.services ? modelFetch)
    && (options.services.modelFetch ? jobs);

  inferredGpuVendor =
    if hasInfernixGpu
    then
      {
        amd = "rocm";
        intel = "xpu";
        nvidia = "cuda";
        rocm = "rocm";
        xpu = "xpu";
        cuda = "cuda";
      }.${
        config.services.infernix.gpu.vendor
      } or "rocm"
    else "rocm";

  defaultPackage =
    if cfg.gpuVendor == "rocm"
    then self.packages.${system}.comfyui-rocm
    else if cfg.gpuVendor == "xpu"
    then self.packages.${system}.comfyui-xpu
    else pkgs.comfyui;

  backendPort =
    if hasInfernixComfyui
    then config.services.infernix.comfyui.port
    else cfg.comfyuiPort;

  extraModelPathsConfig = pkgs.writeText "trellis-workbench-model-paths.yaml" ''
    trellis_workbench:
      base_path: ${cfg.modelsDir}
      is_default: true
      clip_vision: clip_vision
      diffusion_models: diffusion_models
      text_encoders: text_encoders
      vae: vae
      background_removal: background_removal
    ${cfg.extraModelPaths}
  '';
in {
  options.services.trellisWorkbench = {
    enable = mkEnableOption "the TRELLIS.2 ComfyUI backend";

    gpuVendor = mkOption {
      type = types.enum ["rocm" "xpu" "cuda"];
      default = inferredGpuVendor;
      defaultText = lib.literalExpression ''
        inferred from config.services.infernix.gpu.vendor if present, otherwise "rocm"
      '';
      description = "GPU runtime vendor used by ComfyUI and the runtime doctor.";
    };

    package = mkOption {
      type = types.package;
      default = defaultPackage;
      defaultText = lib.literalExpression ''
        comfyui-rocm / comfyui-xpu from trellis-workbench (or pkgs.comfyui for cuda)
      '';
      description = "ComfyUI package whose torch matches this host's GPU (ROCm, XPU, or CUDA).";
    };

    comfyuiPort = mkOption {
      type = types.port;
      default = 8188;
      description = "Loopback port that the underlying ComfyUI service listens on.";
    };

    port = mkOption {
      type = types.port;
      default = 8190;
      description = "External/proxy port exposed by the TRELLIS.2 socket proxy.";
    };

    onDemand = mkOption {
      type = types.bool;
      default = false;
      description = "Start ComfyUI on the first proxy connection and stop it after `idleTimeout`.";
    };

    idleTimeout = mkOption {
      type = types.str;
      default = "20min";
      description = "Idle period after which the on-demand proxy and ComfyUI stop.";
    };

    modelsDir = mkOption {
      type = types.str;
      default = "/var/lib/trellis-workbench/models";
      description = "ComfyUI model folder tree, outside the Nix store and repository.";
    };

    extraModelPaths = mkOption {
      type = types.lines;
      default = "";
      description = "Additional ComfyUI extra-model-paths YAML sections.";
    };

    acceptDinov3License = mkOption {
      type = types.bool;
      default = false;
      description = ''
        Accept Meta's non-OSI DINOv3 License for local TRELLIS.2 conditioning.
        TRELLIS.2 conditioning requires the DINOv3 encoder; when false, the
        DINOv3 model pin is omitted and generation is unavailable (strict-FOSS
        profile).
      '';
    };

    proxy = {
      enable = mkOption {
        type = types.bool;
        default = false;
        description = "Expose the unauthenticated ComfyUI API through a systemd socket proxy.";
      };

      listenAddress = mkOption {
        type = types.nullOr types.str;
        default = null;
        example = "192.0.2.10";
        description = "Address for `trellis-workbench-proxy.socket` to bind (FreeBind enabled).";
      };

      openFirewall = mkOption {
        type = types.bool;
        default = false;
        description = "Open `port` in the NixOS firewall when `proxy.enable` is true.";
      };

      firewallInterface = mkOption {
        type = types.nullOr types.str;
        default = null;
        example = "wg0";
        description = ''
          If set, open `port` only on this firewall interface; otherwise open
          `networking.firewall.allowedTCPPorts`.
        '';
      };
    };

    modelFetch = {
      enable = mkOption {
        type = types.bool;
        default = false;
        description = ''
          Populate `services.modelFetch.jobs` with the pinned TRELLIS.2 and
          BiRefNet model jobs. Requires a NixOS module providing
          `services.modelFetch.jobs`.
        '';
      };
    };

    runtimeDoctor = {
      enable = mkOption {
        type = types.bool;
        default = true;
        description = ''
          Install `trellis-workbench-runtime-doctor` bound to the configured
          ComfyUI package and GPU vendor.
        '';
      };
    };

    resolvedModelPins = mkOption {
      type = types.attrsOf types.anything;
      readOnly = true;
      default = modelPins.jobsFor {
        inherit (cfg) modelsDir acceptDinov3License;
      };
      description = ''
        Resolved TRELLIS.2 and BiRefNet model pin specifications for this host's
        `modelsDir` and `acceptDinov3License` setting, usable by custom fetchers.
      '';
    };
  };

  config = mkIf cfg.enable (lib.mkMerge [
    {
      assertions = [
        {
          assertion = !cfg.proxy.enable || (cfg.proxy.listenAddress != null && cfg.proxy.listenAddress != "");
          message = "services.trellisWorkbench.proxy.listenAddress must be set when services.trellisWorkbench.proxy.enable is true.";
        }
        {
          assertion =
            !cfg.proxy.enable
            || (cfg.proxy.listenAddress != "127.0.0.1" && cfg.proxy.listenAddress != "localhost")
            || cfg.port != backendPort;
          message = "services.trellisWorkbench.port must differ from the loopback ComfyUI port when proxying on loopback.";
        }
        {
          assertion = !cfg.modelFetch.enable || hasModelFetch;
          message = "services.trellisWorkbench.modelFetch.enable requires a NixOS module providing services.modelFetch.jobs.";
        }
      ];

      warnings = lib.optional (!cfg.acceptDinov3License) ''
        services.trellisWorkbench: DINOv3 license not accepted; TRELLIS.2 generation is unavailable on this host.
      '';

      systemd.services.comfyui = mkIf cfg.proxy.enable (
        {
          # Started only once it answers, so the socket-activated proxy never forwards
          # the first request to a port that is not listening yet.
          serviceConfig = {
            ExecStartPost = "${pkgs.curl}/bin/curl -sf -o /dev/null --retry 180 --retry-delay 1 --retry-connrefused --retry-max-time 240 http://127.0.0.1:${toString backendPort}/system_stats";
            TimeoutStartSec = "5min";
          };
        }
        // (
          if cfg.onDemand
          then {unitConfig.StopWhenUnneeded = true;}
          else {wantedBy = ["multi-user.target"];}
        )
      );

      systemd.sockets.trellis-workbench-proxy = mkIf cfg.proxy.enable {
        description = "TRELLIS.2 backend proxy socket";
        wantedBy = ["sockets.target"];
        listenStreams = ["${cfg.proxy.listenAddress}:${toString cfg.port}"];
        socketConfig.FreeBind = true;
      };

      systemd.services.trellis-workbench-proxy = mkIf cfg.proxy.enable {
        description = "Forward TRELLIS.2 requests to loopback ComfyUI";
        requires = ["comfyui.service" "trellis-workbench-proxy.socket"];
        after = ["comfyui.service" "trellis-workbench-proxy.socket"];
        serviceConfig = {
          ExecStart = lib.escapeShellArgs ([
              "${config.systemd.package}/lib/systemd/systemd-socket-proxyd"
              "--connections-max=64"
            ]
            ++ lib.optional cfg.onDemand "--exit-idle-time=${cfg.idleTimeout}"
            ++ ["127.0.0.1:${toString backendPort}"]);
          DynamicUser = true;
          NoNewPrivileges = true;
        };
      };

      networking.firewall.allowedTCPPorts =
        mkIf (cfg.proxy.enable && cfg.proxy.openFirewall && cfg.proxy.firewallInterface == null) [cfg.port];

      networking.firewall.interfaces = mkIf (cfg.proxy.enable && cfg.proxy.openFirewall && cfg.proxy.firewallInterface != null) {
        ${cfg.proxy.firewallInterface}.allowedTCPPorts = [cfg.port];
      };

      # `sudo -u comfyui trellis-workbench-runtime-doctor`: GPU identity, VRAM,
      # device access and a real kernel run with the exact torch the service uses.
      environment.systemPackages = lib.optional cfg.runtimeDoctor.enable (
        self.lib.runtimeDoctor {
          inherit pkgs;
          comfyui = cfg.package;
          vendor = cfg.gpuVendor;
        }
      );
    }

    (lib.optionalAttrs hasInfernixComfyui {
      services.infernix.comfyui = {
        enable = true;
        inherit (cfg) package modelsDir;
        host = "127.0.0.1";
        port = lib.mkDefault cfg.comfyuiPort;
        inherit extraModelPathsConfig;
        # comfyui-xpu ships its own Level Zero driver and IGC (trellis-workbench #2).
        environment = {
          HF_HUB_OFFLINE = "1";
          TRANSFORMERS_OFFLINE = "1";
        };
      };
    })

    (lib.optionalAttrs hasModelFetch {
      services.modelFetch.jobs = mkIf cfg.modelFetch.enable cfg.resolvedModelPins;
    })
  ]);
}
