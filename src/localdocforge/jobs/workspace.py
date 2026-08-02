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

from localdocforge.security.paths import (
    PathSecurityError,
    ensure_contained,
    validate_path_before_access,
)

_WORKSPACE_PREFIX = "ldf-job-"
_SAFE_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_SAFE_TEMP_SUFFIX = re.compile(r"^(?:\.[A-Za-z0-9_-]{1,16})?$")


class CollisionPolicy(StrEnum):
    FAIL = "fail"
    RENAME = "rename"
    OVERWRITE = "overwrite"


class OutputCollisionError(FileExistsError):
    """Destination exists and the collision policy forbids replacing it."""


def default_jobs_root() -> Path:
    """Directory that holds every per-job workspace."""
    return Path(tempfile.gettempdir()) / "localdocforge" / "jobs"


def make_private_dir(path: Path, *, exist_ok: bool = True) -> None:
    """Create a private directory without chmodding a pre-existing parent."""
    path = validate_path_before_access(path, what="private directory")
    created = False
    try:
        path.mkdir(parents=True, exist_ok=False)
        created = True
    except FileExistsError:
        if not exist_ok:
            raise
        if not path.is_dir():
            raise NotADirectoryError(path) from None
    if created and os.name == "posix":
        os.chmod(path, stat.S_IRWXU)  # 0700; on Windows the user temp dir is already per-user


class JobWorkspace:
    """An isolated scratch directory for exactly one job."""

    def __init__(self, job_id: str | None = None, *, root: Path | None = None) -> None:
        self.job_id = uuid.uuid4().hex if job_id is None else job_id
        if not _SAFE_JOB_ID.fullmatch(self.job_id):
            raise ValueError("job_id must contain only ASCII letters, digits, '_' or '-'")
        base = root or default_jobs_root()
        make_private_dir(base)
        self.path = ensure_contained(
            base / f"{_WORKSPACE_PREFIX}{self.job_id}", base, what="job workspace"
        )
        make_private_dir(self.path, exist_ok=False)

    def subdir(self, name: str) -> Path:
        target = ensure_contained(self.path / name, self.path, what="workspace subdir")
        target.mkdir(parents=True, exist_ok=True)
        return target

    def temp_file(self, suffix: str = "") -> Path:
        if not _SAFE_TEMP_SUFFIX.fullmatch(suffix):
            raise ValueError("temporary suffix must be empty or a short ASCII file extension")
        return self.contain(self.path / f"tmp-{uuid.uuid4().hex}{suffix}")

    def contain(self, path: Path) -> Path:
        """Assert that ``path`` stays inside this workspace and return it resolved."""
        return ensure_contained(path, self.path, what="workspace path")

    def cleanup(self) -> bool:
        return remove_tree_with_retries(self.path)

    def __enter__(self) -> JobWorkspace:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.cleanup()


def remove_tree_with_retries(path: Path, attempts: int = 5, delay: float = 0.2) -> bool:
    """Remove a tree with Windows-lock retries and report whether it is gone."""
    for attempt in range(attempts):
        try:
            if path.exists():
                shutil.rmtree(path)
            return not path.exists()
        except OSError:
            if attempt == attempts - 1:
                return not path.exists()  # leave it for the startup sweep
            time.sleep(delay)
    return not path.exists()


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
                if remove_tree_with_retries(entry):
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
        candidate = validate_path_before_access(
            destination.with_name(f"{base_stem} ({counter}){suffix}"),
            what="renamed output path",
        )
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
    source = validate_path_before_access(source, what="publication source")
    if not source.is_file():
        raise FileNotFoundError(f"Nothing to publish: {source}")
    destination = validate_path_before_access(destination, what="publication destination")
    if destination.exists() and destination.is_dir():
        raise IsADirectoryError(f"Destination is a directory: {destination}")
    parent = validate_path_before_access(destination.parent, what="publication directory")
    parent.mkdir(parents=True, exist_ok=True)
    # Repeat after creation so a pre-existing/missing boundary is checked at
    # the point immediately before staging and final publication.
    parent = validate_path_before_access(parent, what="publication directory")
    destination = validate_path_before_access(destination, what="publication destination")

    staging = validate_path_before_access(
        parent / f".ldf-staging-{uuid.uuid4().hex}{destination.suffix}",
        what="publication staging path",
    )
    try:
        with source.open("rb") as src, staging.open("wb") as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
            dst.flush()
            os.fsync(dst.fileno())
        if collision is CollisionPolicy.OVERWRITE:
            final = destination
            os.replace(staging, final)
        else:
            final = destination
            while True:
                try:
                    # Atomic no-replace publication. Unlike os.replace, this
                    # cannot clobber a file created after our collision check.
                    os.link(staging, final)
                    break
                except FileExistsError as exc:
                    if collision is CollisionPolicy.FAIL:
                        raise OutputCollisionError(
                            f"Output already exists: {destination}. "
                            f"Choose --collision rename or --collision overwrite."
                        ) from exc
                    final = _next_free_path(final)
    finally:
        if staging.exists():
            try:
                staging.unlink()
            except OSError:
                pass
    return final


def contained_output_path(destination: Path, allowed_roots: list[Path] | None) -> Path:
    """Validate a user-requested output path against configured allowed roots."""
    checked = validate_path_before_access(destination, what="output path")
    resolved = checked.resolve(strict=False)
    resolved = validate_path_before_access(resolved, what="resolved output path")
    if allowed_roots is not None:
        for root in allowed_roots:
            try:
                return ensure_contained(resolved, root, what="output path")
            except PathSecurityError:
                continue
        raise PathSecurityError("Output path is outside every allowed output root")
    return resolved
