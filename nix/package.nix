# The bridge library + CLI. Runs on client machines; no torch, no GPU.
{
  lib,
  buildPythonPackage,
  setuptools,
  pydantic,
  websockets,
  numpy,
  trimesh,
  pillow,
  scipy,
  aiohttp,
  pytestCheckHook,
}:
buildPythonPackage {
  pname = "trellis-workbench";
  version = "0.1.0";
  pyproject = true;

  src = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../pyproject.toml
      ../README.md
      ../src
      ../tests/unit
      ../tests/conftest.py
      ../tests/assets.py
    ];
  };

  build-system = [setuptools];
  dependencies = [pydantic websockets numpy trimesh pillow scipy];

  nativeCheckInputs = [pytestCheckHook aiohttp];
  enabledTestPaths = ["tests/unit"];
  pythonImportsCheck = ["trellis_workbench"];

  meta = {
    description = "Local TRELLIS.2 (ComfyUI) to Blender asset workbench";
    license = with lib.licenses; [mit asl20];
    mainProgram = "trellis-workbench";
  };
}
