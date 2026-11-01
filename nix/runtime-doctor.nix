# On-host GPU doctor bound to the exact ComfyUI Python/torch the service runs.
#   trellis-workbench-runtime-doctor   (run as the service user: sudo -u comfyui ...)
{
  pkgs,
  comfyui,
  vendor, # "rocm" | "xpu"
}:
pkgs.runCommand "trellis-workbench-runtime-doctor" {
  nativeBuildInputs = [pkgs.makeBinaryWrapper];
  meta.mainProgram = "trellis-workbench-runtime-doctor";
} ''
  makeWrapper ${pkgs.lib.getExe comfyui.passthru.pythonEnv} $out/bin/trellis-workbench-runtime-doctor \
    --add-flags ${../src/trellis_workbench/doctor_runtime.py} \
    --set TRELLIS_WORKBENCH_VENDOR ${vendor}${pkgs.lib.optionalString (vendor == "xpu") ''
    \
      --set-default ZE_ENABLE_ALT_DRIVERS ${comfyui.passthru.xpuDriverEnv.ZE_ENABLE_ALT_DRIVERS} \
      --prefix LD_LIBRARY_PATH : ${comfyui.passthru.xpuDriverEnv.LD_LIBRARY_PATH}''}
''
