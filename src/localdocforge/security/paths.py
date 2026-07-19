"""Path containment checks used everywhere a path crosses a trust boundary."""

from __future__ import annotations

import contextlib
import os
from pathlib import Path


class PathSecurityError(Exception):
    """A path escaped its allowed root or is otherwise unsafe."""


def is_remote_path(path: Path) -> bool:
    """Detect path forms that can trigger network filesystem traffic.

    UNC/device paths are rejected lexically, before any stat/resolve call.
    On Windows, mapped network drives are detected with ``GetDriveTypeW``.
    """
    raw = os.fspath(path)
    normalized = raw.replace("/", "\\")
    if normalized.startswith("\\\\"):
        return True
    if os.name == "nt":
        drive, _tail = os.path.splitdrive(raw)
        if len(drive) == 2 and drive[1] == ":":
            with contextlib.suppress(AttributeError, OSError):
                import ctypes

                drive_type = ctypes.windll.kernel32.GetDriveTypeW(f"{drive}\\")  # type: ignore[attr-defined]
                return drive_type == 4  # DRIVE_REMOTE
    return False


def is_within(path: Path, root: Path) -> bool:
    """True if ``path`` (fully resolved, following symlinks) is inside ``root``."""
    try:
        resolved_path = path.resolve(strict=False)
        resolved_root = root.resolve(strict=False)
    except OSError:
        return False
    return resolved_path == resolved_root or resolved_root in resolved_path.parents


def ensure_contained(path: Path, root: Path, *, what: str = "path") -> Path:
    """Return the resolved path, or raise if it escapes ``root``.

    The resolved form is returned so callers operate on the canonical path and
    cannot be redirected later through symlinks or ``..`` segments.
    """
    resolved = path.resolve(strict=False)
    if not is_within(resolved, root):
        raise PathSecurityError(f"{what} escapes its allowed directory")
    return resolved
