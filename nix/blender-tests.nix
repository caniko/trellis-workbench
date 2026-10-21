# Headless Blender acceptance tests on procedural fixtures (no models, no GPU).
{
  lib,
  runCommand,
  blender,
  python3,
  trellis-workbench-blender-addon,
  src,
}: let
  py = python3.withPackages (ps: [ps.trellis-workbench]);
in
  runCommand "trellis-workbench-blender-tests" {nativeBuildInputs = [blender py];} ''
    export HOME=$TMPDIR/home XDG_CONFIG_HOME=$TMPDIR/config
    mkdir -p $HOME $out
    cp -r ${lib.cleanSource src}/{tests,blender_addon} .
    chmod -R u+w .
    python tests/blender/make_fixtures.py $TMPDIR/fixtures > fixtures.json
    BLENDER_SYSTEM_EXTENSIONS=${trellis-workbench-blender-addon}/${trellis-workbench-blender-addon.systemExtensions} \
      blender --background --factory-startup --python-exit-code 1 \
        --python tests/blender/run.py -- fixtures.json $out
  ''
