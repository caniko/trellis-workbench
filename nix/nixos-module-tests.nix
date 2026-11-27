# Evaluation tests for `nixosModules.default` (`services.trellisWorkbench`).
{
  lib,
  nixpkgs,
  pkgs,
  self,
  system,
}: let
  # Lightweight ComfyUI stand-in so evaluation tests never build PyTorch/ComfyUI.
  mockComfyui =
    pkgs.runCommand "mock-comfyui" {
      passthru = {
        pythonEnv = pkgs.python3;
        xpuDriverEnv = {
          ZE_ENABLE_ALT_DRIVERS = "/dev/null";
          OCL_ICD_FILENAMES = "/dev/null";
          LD_LIBRARY_PATH = "/dev/null";
        };
      };
    } ''
      mkdir -p $out/bin
      touch $out/bin/comfyui
    '';

  # Minimal stub declaring `services.modelFetch.jobs`.
  modelFetchStub = {lib, ...}: {
    options.services.modelFetch.jobs = lib.mkOption {
      type = lib.types.attrsOf lib.types.anything;
      default = {};
    };
  };

  # Minimal stub declaring `services.infernix.{comfyui,gpu}`.
  infernixStub = {lib, ...}: {
    options.services.infernix = {
      gpu.vendor = lib.mkOption {
        type = lib.types.enum ["amd" "intel" "nvidia"];
        default = "amd";
      };
      comfyui = {
        enable = lib.mkEnableOption "comfyui";
        package = lib.mkOption {type = lib.types.package;};
        modelsDir = lib.mkOption {type = lib.types.str;};
        host = lib.mkOption {type = lib.types.str;};
        port = lib.mkOption {
          type = lib.types.port;
          default = 8188;
        };
        extraModelPathsConfig = lib.mkOption {type = lib.types.path;};
        environment = lib.mkOption {
          type = lib.types.attrsOf lib.types.str;
          default = {};
        };
      };
    };
  };

  evalConfig = extraModules:
    (nixpkgs.lib.nixosSystem {
      inherit system;
      modules =
        [
          self.nixosModules.default
          {
            # Keep evaluation self-contained and warning-free.
            system.stateVersion = "25.05";
            fileSystems."/" = {
              device = "nodev";
              fsType = "ext4";
            };
            boot.loader.grub.device = "nodev";
          }
        ]
        ++ extraModules;
    }).config;

  failedAssertions = cfg:
    map (a: a.message) (lib.filter (a: !a.assertion) cfg.assertions);

  # Test 1: default / always-on configuration with custom proxy + firewallInterface.
  t1 = evalConfig [
    {
      services.trellisWorkbench = {
        enable = true;
        package = mockComfyui;
        gpuVendor = "rocm";
        acceptDinov3License = true;
        proxy = {
          enable = true;
          listenAddress = "192.0.2.10";
          openFirewall = true;
          firewallInterface = "wg0";
        };
      };
    }
  ];

  # Test 2: onDemand = true configuration (socket + proxy + StopWhenUnneeded).
  t2 = evalConfig [
    {
      services.trellisWorkbench = {
        enable = true;
        package = mockComfyui;
        gpuVendor = "xpu";
        onDemand = true;
        idleTimeout = "15min";
        acceptDinov3License = true;
        proxy = {
          enable = true;
          listenAddress = "192.0.2.10";
          openFirewall = true;
        };
      };
    }
  ];

  # Test 3: DINOv3 gate behavior when acceptDinov3License = false.
  t3 = evalConfig [
    modelFetchStub
    {
      services.trellisWorkbench = {
        enable = true;
        package = mockComfyui;
        gpuVendor = "cuda";
        acceptDinov3License = false;
        modelFetch.enable = true;
      };
    }
  ];

  # Test 4: modelFetch.enable = true with stub populates all expected pinned jobs.
  t4 = evalConfig [
    modelFetchStub
    {
      services.trellisWorkbench = {
        enable = true;
        package = mockComfyui;
        gpuVendor = "rocm";
        modelsDir = "/data/trellis/models";
        acceptDinov3License = true;
        modelFetch.enable = true;
      };
    }
  ];

  # Test 4b: modelFetch.enable = true without a modelFetch module fails assertion.
  t4Missing = evalConfig [
    {
      services.trellisWorkbench = {
        enable = true;
        package = mockComfyui;
        gpuVendor = "rocm";
        acceptDinov3License = true;
        modelFetch.enable = true;
      };
    }
  ];

  # Test 5: infernix integration (auto gpuVendor + comfyui wiring + proxy disabled).
  t5 = evalConfig [
    infernixStub
    {
      services.infernix.gpu.vendor = "intel";
      services.trellisWorkbench = {
        enable = true;
        package = mockComfyui;
        acceptDinov3License = true;
        extraModelPaths = ''
          custom_loras:
            loras: /var/lib/trellis-workbench/models/loras
        '';
      };
    }
  ];

  t1ProxyExec = t1.systemd.services.trellis-workbench-proxy.serviceConfig.ExecStart;
  t2ProxyExec = t2.systemd.services.trellis-workbench-proxy.serviceConfig.ExecStart;
  t2ProxyCfg = t2.systemd.services.trellis-workbench-proxy.serviceConfig;
  t2Socket = t2.systemd.sockets.trellis-workbench-proxy;
  t2Comfy = t2.systemd.services.comfyui;

  hasDinov3 = files: lib.any (f: f.name == "clip_vision/dino_v3_vit_l.safetensors") files;

  failures = lib.concatStringsSep "\n" (lib.filter (s: s != "") [
    # Test 1 checks
    (lib.optionalString (failedAssertions t1 != [])
      "test1: unexpected failed assertions: ${lib.concatStringsSep "; " (failedAssertions t1)}")
    (lib.optionalString (t1.warnings != [])
      "test1: unexpected warnings when acceptDinov3License = true")
    (lib.optionalString (t1.services.trellisWorkbench.modelsDir != "/var/lib/trellis-workbench/models")
      "test1: default modelsDir must be /var/lib/trellis-workbench/models")
    (lib.optionalString (t1.systemd.sockets.trellis-workbench-proxy.listenStreams != ["192.0.2.10:8190"])
      "test1: proxy socket must listen on 192.0.2.10:8190")
    (lib.optionalString (!(lib.elem 8190 t1.networking.firewall.interfaces.wg0.allowedTCPPorts))
      "test1: port 8190 must be open on firewallInterface wg0")
    (lib.optionalString (lib.elem 8190 t1.networking.firewall.allowedTCPPorts)
      "test1: port 8190 must not be opened globally when firewallInterface is set")
    (lib.optionalString (!(lib.elem "multi-user.target" t1.systemd.services.comfyui.wantedBy))
      "test1: always-on comfyui must be wantedBy multi-user.target")
    (lib.optionalString (t1.systemd.services.comfyui.unitConfig.StopWhenUnneeded or false)
      "test1: always-on comfyui must not set StopWhenUnneeded")
    (lib.optionalString (lib.hasInfix "--exit-idle-time=" t1ProxyExec)
      "test1: always-on proxy must not pass --exit-idle-time")
    (lib.optionalString (!(lib.any (p: (p.name or "") == "trellis-workbench-runtime-doctor") t1.environment.systemPackages))
      "test1: runtimeDoctor must be installed in environment.systemPackages")

    # Test 2 checks
    (lib.optionalString (failedAssertions t2 != [])
      "test2: unexpected failed assertions: ${lib.concatStringsSep "; " (failedAssertions t2)}")
    (lib.optionalString (!(t2Socket.socketConfig.FreeBind or false))
      "test2: proxy socket must set FreeBind = true")
    (lib.optionalString (!(lib.hasInfix "systemd-socket-proxyd" t2ProxyExec))
      "test2: proxy ExecStart must invoke systemd-socket-proxyd")
    (lib.optionalString (!(lib.hasInfix "--exit-idle-time=15min" t2ProxyExec))
      "test2: onDemand proxy must pass --exit-idle-time=15min")
    (lib.optionalString (!(t2ProxyCfg.DynamicUser or false) || !(t2ProxyCfg.NoNewPrivileges or false))
      "test2: proxy service must set DynamicUser = true and NoNewPrivileges = true")
    (lib.optionalString (!(t2Comfy.unitConfig.StopWhenUnneeded or false))
      "test2: onDemand comfyui must set StopWhenUnneeded = true")
    (lib.optionalString (lib.elem "multi-user.target" t2Comfy.wantedBy)
      "test2: onDemand comfyui must not be wantedBy multi-user.target")
    (lib.optionalString (!(lib.hasInfix "/system_stats" (t2Comfy.serviceConfig.ExecStartPost or "")))
      "test2: comfyui must set ExecStartPost healthcheck against /system_stats")
    (lib.optionalString (!(lib.hasInfix (builtins.unsafeDiscardStringContext "${mockComfyui}/bin/comfyui") (t2Comfy.serviceConfig.ExecStart or "")))
      "test2: standalone proxy must start the configured ComfyUI package")
    (lib.optionalString (!(lib.elem 8190 t2.networking.firewall.allowedTCPPorts))
      "test2: openFirewall without firewallInterface must open global allowedTCPPorts")

    # Test 3 checks
    (lib.optionalString (failedAssertions t3 != [])
      "test3: unexpected failed assertions: ${lib.concatStringsSep "; " (failedAssertions t3)}")
    (lib.optionalString (!(lib.any (w: lib.hasInfix "DINOv3 license not accepted" w) t3.warnings))
      "test3: acceptDinov3License = false must emit DINOv3 warning")
    (lib.optionalString (hasDinov3 t3.services.trellisWorkbench.resolvedModelPins.trellis2.files)
      "test3: resolvedModelPins must exclude DINOv3 when acceptDinov3License = false")
    (lib.optionalString (hasDinov3 t3.services.modelFetch.jobs.trellis2.files)
      "test3: modelFetch.jobs.trellis2 must exclude DINOv3 when acceptDinov3License = false")
    (lib.optionalString (builtins.length t3.services.modelFetch.jobs.trellis2.files != 4)
      "test3: expected 4 base TRELLIS.2 files without DINOv3")

    # Test 4 checks
    (lib.optionalString (failedAssertions t4 != [])
      "test4: unexpected failed assertions: ${lib.concatStringsSep "; " (failedAssertions t4)}")
    (lib.optionalString (!(lib.elem "multi-user.target" t4.systemd.services.comfyui.wantedBy)
      || !(lib.hasInfix "--extra-model-paths-config" t4.systemd.services.comfyui.serviceConfig.ExecStart))
    "test4: standalone ComfyUI without a proxy must start at boot with configured model paths")
    (lib.optionalString (!(t4.services.modelFetch.jobs ? trellis2 && t4.services.modelFetch.jobs ? trellis-birefnet))
      "test4: modelFetch.jobs must populate both trellis2 and trellis-birefnet")
    (lib.optionalString (!(hasDinov3 t4.services.modelFetch.jobs.trellis2.files))
      "test4: modelFetch.jobs.trellis2 must include DINOv3 when acceptDinov3License = true")
    (lib.optionalString (builtins.length t4.services.modelFetch.jobs.trellis2.files != 5)
      "test4: expected 5 TRELLIS.2 files with DINOv3 accepted")
    (lib.optionalString (t4.services.modelFetch.jobs.trellis2.destDir != "/data/trellis/models" || t4.services.modelFetch.jobs.trellis-birefnet.destDir != "/data/trellis/models")
      "test4: modelFetch jobs must inherit modelsDir")
    (lib.optionalString (t4.services.modelFetch.jobs.trellis-birefnet.repo != "Comfy-Org/BiRefNet" || builtins.length t4.services.modelFetch.jobs.trellis-birefnet.files != 1)
      "test4: trellis-birefnet job must pin Comfy-Org/BiRefNet")
    (lib.optionalString (!(lib.any (m: lib.hasInfix "services.modelFetch.jobs" m) (failedAssertions t4Missing)))
      "test4b: modelFetch.enable = true without services.modelFetch module must fail assertion")

    # Test 5 checks
    (lib.optionalString (failedAssertions t5 != [])
      "test5: unexpected failed assertions: ${lib.concatStringsSep "; " (failedAssertions t5)}")
    (lib.optionalString (t5.services.trellisWorkbench.gpuVendor != "xpu")
      "test5: gpuVendor must auto-detect xpu from services.infernix.gpu.vendor = intel")
    (lib.optionalString (!t5.services.infernix.comfyui.enable || t5.services.infernix.comfyui.host != "127.0.0.1" || t5.services.infernix.comfyui.port != 8188)
      "test5: services.infernix.comfyui must be enabled on 127.0.0.1:8188")
    (lib.optionalString ((t5.services.infernix.comfyui.environment.HF_HUB_OFFLINE or "") != "1" || (t5.services.infernix.comfyui.environment.TRANSFORMERS_OFFLINE or "") != "1")
      "test5: services.infernix.comfyui must enforce offline HF/Transformers env vars")
    (lib.optionalString (t5.systemd.sockets ? trellis-workbench-proxy || t5.systemd.services ? trellis-workbench-proxy)
      "test5: proxy.enable = false must not create trellis-workbench-proxy units")
  ]);
in
  pkgs.runCommand "trellis-workbench-nixos-module-eval" {} ''
    failures=${lib.escapeShellArg failures}
    if [ -n "$failures" ]; then
      printf '%s\n' "$failures" >&2
      exit 1
    fi
    touch $out
  ''
