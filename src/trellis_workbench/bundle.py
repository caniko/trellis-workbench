"""Immutable asset bundles: staged, validated, then atomically published.

Layout: <root>/<asset_id>/r0001/{manifest.json, input/, source/, working/, previews/, reports/, logs/}
A revision directory appears only through one rename(RENAME_NOREPLACE) of a fully written,
checksummed staging directory, so a reader never sees a partial bundle and an existing
revision is never overwritten.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import re
import shutil
import uuid
from pathlib import Path, PurePosixPath

from .schema import FileRecord, Manifest

REVISION_RE = re.compile(r"^r(\d{4,})$")
ASSET_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_AT_FDCWD = -100
_RENAME_NOREPLACE = 1


class BundleError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def safe_relpath(rel: str) -> PurePosixPath:
    p = PurePosixPath(rel)
    if not rel or p.is_absolute() or ".." in p.parts or "\\" in rel or "\0" in rel or p.parts[0] in {".", ""}:
        raise BundleError(f"unsafe bundle path {rel!r}")
    return p


def check_root(root: Path, allowed: list[Path]) -> Path:
    """The bundle root must resolve inside an allowlisted directory."""
    resolved = root.expanduser().resolve()
    for base in allowed:
        b = base.expanduser().resolve()
        if resolved == b or b in resolved.parents:
            return resolved
    raise BundleError(f"asset root {resolved} is outside the allowed roots {[str(a) for a in allowed]}")


def _rename_noreplace(src: Path, dst: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    rc = libc.renameat2(_AT_FDCWD, os.fsencode(src), _AT_FDCWD, os.fsencode(dst), _RENAME_NOREPLACE)
    if rc != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), str(dst))


def revisions(root: Path, asset_id: str) -> list[int]:
    d = root / asset_id
    if not d.is_dir():
        return []
    return sorted(int(m.group(1)) for p in d.iterdir() if (m := REVISION_RE.match(p.name)) and p.is_dir())


class Staging:
    """A private, writable bundle under construction inside <root>/.staging."""

    def __init__(self, root: Path, asset_id: str):
        if not ASSET_ID_RE.match(asset_id):
            raise BundleError(f"invalid asset id {asset_id!r}")
        self.root = root
        self.asset_id = asset_id
        self.dir = root / ".staging" / uuid.uuid4().hex
        self.dir.mkdir(parents=True)
        self.files: list[FileRecord] = []

    def path(self, rel: str) -> Path:
        target = self.dir.joinpath(*safe_relpath(rel).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def add_bytes(self, rel: str, data: bytes, role: str) -> FileRecord:
        target = self.path(rel)
        with target.open("xb") as f:  # never overwrite inside a bundle either
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        return self._record(rel, target, role)

    def add_file(self, rel: str, src: Path, role: str) -> FileRecord:
        target = self.path(rel)
        shutil.copyfile(src, target)
        return self._record(rel, target, role)

    def _record(self, rel: str, target: Path, role: str) -> FileRecord:
        rec = FileRecord(path=str(safe_relpath(rel)), sha256=sha256_file(target), size=target.stat().st_size, role=role)
        self.files.append(rec)
        return rec

    def publish(self, manifest: Manifest) -> Path:
        """Write the manifest, verify everything, fsync, and atomically claim the next revision."""
        if manifest.asset_id != self.asset_id:
            raise BundleError("manifest asset id does not match the staging bundle")
        listed = {f.path for f in manifest.files}
        if listed != {f.path for f in self.files}:
            raise BundleError("manifest file list differs from the staged files")
        mpath = self.dir / "manifest.json"
        with mpath.open("x") as f:
            f.write(manifest.model_dump_json(indent=2))
            f.flush()
            os.fsync(f.fileno())
        verify(self.dir)
        _fsync_tree(self.dir)
        asset_dir = self.root / self.asset_id
        asset_dir.mkdir(parents=True, exist_ok=True)
        rev = manifest.revision
        target = asset_dir / f"r{rev:04d}"
        try:
            _rename_noreplace(self.dir, target)
        except OSError as exc:
            if exc.errno in (errno.EEXIST, errno.ENOTEMPTY):
                raise BundleError(f"revision {rev} of {self.asset_id} already exists; refusing to overwrite") from exc
            raise
        _fsync_dir(asset_dir)
        return target

    def discard(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


def _fsync_dir(d: Path) -> None:
    fd = os.open(d, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_tree(d: Path) -> None:
    for sub, _dirs, _files in os.walk(d):
        _fsync_dir(Path(sub))


def load(bundle_dir: Path) -> Manifest:
    return Manifest.model_validate_json((bundle_dir / "manifest.json").read_text())


def verify(bundle_dir: Path) -> Manifest:
    """Parse the manifest and check every listed file's size and sha256; reject extras."""
    manifest = load(bundle_dir)
    seen = set()
    for rec in manifest.files:
        p = bundle_dir.joinpath(*safe_relpath(rec.path).parts)
        if p.is_symlink() or not p.is_file():
            raise BundleError(f"missing or non-regular file {rec.path}")
        if p.stat().st_size != rec.size or sha256_file(p) != rec.sha256:
            raise BundleError(f"checksum mismatch for {rec.path}")
        seen.add(rec.path)
    for sub, _dirs, files in os.walk(bundle_dir):
        for name in files:
            rel = Path(sub, name).relative_to(bundle_dir).as_posix()
            if rel != "manifest.json" and rel not in seen:
                raise BundleError(f"unlisted file in bundle: {rel}")
    return manifest


def latest(root: Path, asset_id: str) -> Path | None:
    revs = revisions(root, asset_id)
    return root / asset_id / f"r{revs[-1]:04d}" if revs else None


def list_bundles(root: Path) -> list[dict]:
    out = []
    if not root.is_dir():
        return out
    for asset in sorted(root.iterdir()):
        if not ASSET_ID_RE.match(asset.name):
            continue
        for rev in revisions(root, asset.name):
            d = asset / f"r{rev:04d}"
            try:
                m = load(d)
            except (OSError, ValueError):
                continue
            out.append(
                {
                    "asset_id": m.asset_id,
                    "revision": m.revision,
                    "display_name": m.display_name,
                    "category": m.category.value,
                    "status": m.status.value,
                    "path": str(d),
                    "working_glb": next((f.path for f in m.files if f.role == "working-glb"), None),
                }
            )
    return out


def clean_stale_staging(root: Path, older_than_s: float = 24 * 3600) -> int:
    """Remove abandoned staging directories (never published, never visible). Recent ones may
    belong to a job still running in another process, so only old ones are removed."""
    import time

    stale = root / ".staging"
    n = 0
    if stale.is_dir():
        cutoff = time.time() - older_than_s
        for d in stale.iterdir():
            if d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                n += 1
    return n


def dumps(obj) -> str:
    return json.dumps(obj, indent=2, sort_keys=True, default=str)
