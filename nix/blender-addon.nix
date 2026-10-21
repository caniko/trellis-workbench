# The add-on as a read-only Blender system extension repository:
#   BLENDER_SYSTEM_EXTENSIONS=$out/share/trellis-workbench/extensions
# (Blender loads it as module bl_ext.system.trellis_workbench).
{runCommand}:
runCommand "trellis-workbench-blender-addon-0.1.0" {
  passthru.systemExtensions = "share/trellis-workbench/extensions";
} ''
  mkdir -p $out/share/trellis-workbench/extensions/system
  cp -r ${../blender_addon/trellis_workbench} $out/share/trellis-workbench/extensions/system/trellis_workbench
''
