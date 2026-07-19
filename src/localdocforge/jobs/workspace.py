"""Per-job isolated workspaces and atomic output publication.

Every job gets a private directory under the jobs root. All intermediate
files stay inside it; nothing is written to the destination until validation
passed, and the final move is atomic on the destination volume. Workspaces
are removed on success, failure, and cancellation, and stale ones from
interrupted sessions are swept at the next startup.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import tempfile
import time
import uuid
from enum import StrEnum
from pathlib import Path

from localdocforge.security.paths import PathSecurityError, ensure_contained

_WORKSPACE_PREFIX = "ldf-job-"


class CollisionPolicy(StrEnum):
    FAIL = "fail"
    RENAME = "rename"
    OVERWRITE = "overwrite"


class OutputCollisionError(FileExistsError):
    """Destination exists and the collision policy forbids replacing it."""


def default_jobs_root() -> Path:
    """Directory that holds every per-job workspace."""
    return Path(tempfile.gettempdir()) / "localdocforge" / "jobs"


def _make_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(path, stat.S_IRWXU)  # 0700; on Windows the user temp dir is already per-user


class JobWorkspace:
    """An isolated scratch directory for exactly one job."""

    def __init__(self, job_id: str | None = None, *, root: Path | None = None) -> None:
        self.job_id = job_id or uuid.uuid4().hex
        base = root or default_jobs_root()
        _make_private_dir(base)
        self.path = base / f"{_WORKSPACE_PREFIX}{self.job_id}"
        _make_private_dir(self.path)

    def subdir(self, name: str) -> Path:
        target = ensure_contained(self.path / name, self.path, what="workspace subdir")
        target.mkdir(parents=True, exist_ok=True)
        return target

    def temp_file(self, suffix: str = "") -> Path:
        return self.path / f"tmp-{uuid.uuid4().hex}{suffix}"

    def contain(self, path: Path) -> Path:
        """Assert that ``path`` stays inside this workspace and return it resolved."""
        return ensure_contained(path, self.path, what="workspace path")

    def cleanup(self) -> None:
        _remove_tree_with_retries(self.path)

    def __enter__(self) -> JobWorkspace:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.cleanup()


def _remove_tree_with_retries(path: Path, attempts: int = 5, delay: float = 0.2) -> None:
    """Best-effort recursive removal; retries cover transient Windows file locks."""
    for attempt in range(attempts):
        try:
            if path.exists():
                shutil.rmtree(path)
            return
        except OSError:
            if attempt == attempts - 1:
                return  # leave it for the startup sweep rather than crash the app
            time.sleep(delay)


def cleanup_stale_workspaces(
    root: Path | None = None, *, max_age_seconds: float = 24 * 3600
) -> int:
    """Remove leftover workspaces from interrupted sessions. Returns count removed."""
    base = root or default_jobs_root()
    if not base.is_dir():
        return 0
    removed = 0
    cutoff = time.time() - max_age_seconds
    for entry in base.iterdir():
        if not entry.name.startswith(_WORKSPACE_PREFIX) or not entry.is_dir():
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                _remove_tree_with_retries(entry)
                removed += 1
        except OSError:
            continue
    return removed


_RENAME_PATTERN = re.compile(r"^(?P<stem>.*) \((?P<n>\d+)\)$")


def _next_free_path(destination: Path) -> Path:
    stem, suffix = destination.stem, destination.suffix
    match = _RENAME_PATTERN.match(stem)
    base_stem = match.group("stem") if match else stem
    counter = int(match.group("n")) + 1 if match else 1
    while True:
        candidate = destination.with_name(f"{base_stem} ({counter}){suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def atomic_publish(
    source: Path,
    destination: Path,
    *,
    collision: CollisionPolicy = CollisionPolicy.FAIL,
) -> Path:
    """Atomically move a validated ``source`` file to ``destination``.

    The file is first copied to a hidden temp name in the destination
    directory (same volume), fsynced, then ``os.replace``d into place, so a
    crash can never leave a half-written output at the final name. Returns the
    actual path written (which differs from ``destination`` under ``rename``).
    """
    if not source.is_file():
        raise FileNotFoundError(f"Nothing to publish: {source}")
    destination = destination.expanduser()
    if destination.exists() and destination.is_dir():
        raise IsADirectoryError(f"Destination is a directory: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)

    final = destination
    if destination.exists():
        if collision is CollisionPolicy.FAIL:
            raise OutputCollisionError(
                f"Output already exists: {destination}. "
                f"Choose --collision rename or --collision overwrite."
            )
        if collision is CollisionPolicy.RENAME:
            final = _next_free_path(destination)

    staging = destination.parent / f".ldf-staging-{uuid.uuid4().hex}{destination.suffix}"
    try:
        with source.open("rb") as src, staging.open("wb") as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(staging, final)
    finally:
        if staging.exists():
            try:
                staging.unlink()
            except OSError:
                pass
    return final


def contained_output_path(destination: Path, allowed_roots: list[Path] | None) -> Path:
    """Validate a user-requested output path against configured allowed roots."""
    resolved = destination.expanduser().resolve(strict=False)
    if allowed_roots:
        for root in allowed_roots:
            try:
                return ensure_contained(resolved, root, what="output path")
            except PathSecurityError:
                continue
        raise PathSecurityError("Output path is outside every allowed output root")
    return resolved
