"""The no-OOM-fallback switch: config file and environment."""

from __future__ import annotations

from trellis_workbench import config as configlib
from trellis_workbench.config import Config

ENV = "TRELLIS_WORKBENCH_NO_OOM_FALLBACK"


def test_env_switch(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    assert Config().no_oom_fallback is False  # the interactive default is unchanged
    monkeypatch.setenv(ENV, "1")
    assert Config().no_oom_fallback is True
    monkeypatch.setenv(ENV, "0")
    assert Config().no_oom_fallback is False


def test_config_key_and_env_switch(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    monkeypatch.delenv(ENV, raising=False)
    path.write_text('license_profile = "mixed"\nno_oom_fallback = true\n')
    assert configlib.load(path).no_oom_fallback is True
    path.write_text('license_profile = "mixed"\nno_oom_fallback = false\n')
    assert configlib.load(path).no_oom_fallback is False
    monkeypatch.setenv(ENV, "1")
    assert configlib.load(path).no_oom_fallback is True  # a deployment cannot be loosened by the file
