"""Client configuration: backends, asset root and its allowlist, journal location.

Read from $XDG_CONFIG_HOME/trellis-workbench/config.toml (written by the Home Manager module):

    default_backend = "atlas"
    asset_root = "~/3d/trellis-assets"
    allowed_roots = ["~/3d"]
    license_profile = "mixed"
    [backends]
    atlas = "http://10.123.0.5:8190"
    murph = "http://10.123.0.6:8190"
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


def _xdg(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / default)


@dataclass(frozen=True)
class Config:
    backends: dict[str, str] = field(default_factory=lambda: {"local": "http://127.0.0.1:8188"})
    default_backend: str = "local"
    asset_root: Path = field(default_factory=lambda: _xdg("XDG_DATA_HOME", ".local/share") / "trellis-workbench/assets")
    allowed_roots: tuple[Path, ...] = ()
    journal: Path = field(
        default_factory=lambda: _xdg("XDG_STATE_HOME", ".local/state") / "trellis-workbench/journal.sqlite"
    )
    license_profile: str = "strict-foss"

    def backend_url(self, name_or_url: str | None) -> str:
        key = name_or_url or self.default_backend
        if "://" in key:
            return key
        if key not in self.backends:
            raise SystemExit(f"unknown backend {key!r}; configured: {', '.join(self.backends)}")
        return self.backends[key]

    def roots(self) -> list[Path]:
        return list(self.allowed_roots) or [self.asset_root]


def load(path: Path | None = None) -> Config:
    path = path or _xdg("XDG_CONFIG_HOME", ".config") / "trellis-workbench/config.toml"
    if not path.is_file():
        return Config()
    raw = tomllib.loads(path.read_text())
    base = Config()
    return Config(
        backends=dict(raw.get("backends", base.backends)),
        default_backend=raw.get("default_backend", base.default_backend),
        asset_root=Path(raw["asset_root"]).expanduser() if "asset_root" in raw else base.asset_root,
        allowed_roots=tuple(Path(p).expanduser() for p in raw.get("allowed_roots", [])),
        journal=Path(raw["journal"]).expanduser() if "journal" in raw else base.journal,
        license_profile=raw.get("license_profile", base.license_profile),
    )
