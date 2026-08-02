"""Path containment checks used everywhere a path crosses a trust boundary."""

from __future__ import annotations

import contextlib
import ntpath
import os
import re
import stat
import sys
from pathlib import Path


class PathSecurityError(Exception):
    """A path escaped its allowed root or is otherwise unsafe."""


_WINDOWS_DEVICE_NAME = re.compile(
    r"^(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|CLOCK\$|COM[1-9¹²³]|LPT[1-9¹²³])$",
    re.IGNORECASE,
)
_WINDOWS_EXTENDED_PREFIX = "\\\\?\\"
_WINDOWS_DEVICE_PREFIX = "\\\\.\\"
_DRIVE_REMOTE = 4
_CONFIRMED_LOCAL_DRIVE_TYPES = frozenset({2, 3, 5, 6})


def _windows_drive_type(root: str) -> int | None:
    """Return ``GetDriveTypeW`` for a drive root, or ``None`` if unavailable."""
    if sys.platform != "win32":
        # Same outcome the AttributeError branch below produced, but explicit:
        # non-Windows mypy platforms must not analyze the WinDLL attribute.
        return None
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetDriveTypeW.argtypes = (ctypes.c_wchar_p,)
        kernel32.GetDriveTypeW.restype = ctypes.c_uint
        return int(kernel32.GetDriveTypeW(root))
    except (AttributeError, OSError):
        return None


def _windows_drive_root(normalized: str) -> str | None:
    """Return the ordinary drive root behind a normal or ``\\?\\`` path."""
    upper = normalized.upper()
    if upper.startswith(_WINDOWS_EXTENDED_PREFIX.upper()):
        remainder = normalized[len(_WINDOWS_EXTENDED_PREFIX) :]
        if re.match(r"^[A-Za-z]:\\", remainder):
            return f"{remainder[:2]}\\"
        return None
    drive, _tail = ntpath.splitdrive(normalized)
    if re.fullmatch(r"[A-Za-z]:", drive):
        return f"{drive}\\"
    return None


def _validate_windows_path_form(path: Path, *, what: str) -> None:
    """Reject Win32 namespace and filename aliases before filesystem access."""
    normalized = os.fspath(path).replace("/", "\\")
    upper = normalized.upper()
    if upper.startswith(_WINDOWS_DEVICE_PREFIX.upper()) or upper.startswith("\\??\\"):
        raise PathSecurityError(f"{what} uses a Windows device path")
    if upper.startswith(_WINDOWS_EXTENDED_PREFIX.upper()):
        remainder = normalized[len(_WINDOWS_EXTENDED_PREFIX) :]
        if not (
            remainder.upper().startswith("UNC\\")
            or re.match(r"^[A-Za-z]:\\", remainder)
        ):
            raise PathSecurityError(f"{what} uses an unsupported Windows device namespace")

    drive, tail = ntpath.splitdrive(normalized)
    if ":" in tail:
        raise PathSecurityError(f"{what} uses NTFS alternate-data-stream syntax")
    if drive and not (
        re.fullmatch(r"[A-Za-z]:", drive)
        or drive.upper().startswith(_WINDOWS_EXTENDED_PREFIX.upper())
        or drive.startswith("\\\\")
    ):
        raise PathSecurityError(f"{what} has an invalid Windows drive prefix")

    for component in tail.split("\\"):
        if not component or component in {".", ".."}:
            continue
        if component.endswith((".", " ")):
            raise PathSecurityError(f"{what} contains a trailing-dot or trailing-space alias")
        device_stem = component.split(".", 1)[0].rstrip(" .")
        if _WINDOWS_DEVICE_NAME.fullmatch(device_stem):
            raise PathSecurityError(f"{what} contains a reserved Windows device name")


def _reject_windows_reparse_points(path: Path, *, what: str) -> None:
    """Reject an existing reparse component without following it first."""
    try:
        absolute = path if path.is_absolute() else Path(os.path.abspath(path))
    except OSError as exc:
        raise PathSecurityError(f"{what} could not be checked safely") from exc

    # Inspect root-to-leaf. Once an ordinary missing component is found, every
    # descendant is also missing and can be created without traversing a link.
    chain = list(reversed((absolute, *absolute.parents)))
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    for component in chain:
        try:
            metadata = component.lstat()
        except FileNotFoundError:
            break
        except OSError as exc:
            raise PathSecurityError(f"{what} could not be checked safely") from exc
        if int(getattr(metadata, "st_file_attributes", 0)) & reparse_flag:
            raise PathSecurityError(f"{what} traverses a Windows reparse point")


def validate_path_before_access(
    path: Path,
    *,
    what: str = "path",
    require_local: bool = False,
    reject_reparse: bool = True,
) -> Path:
    """Validate a path lexically and, on Windows, reject reparse traversal.

    Remote recognition happens before any metadata query when ``require_local``
    is set, so strict-offline callers do not contact UNC or mapped-drive roots.
    POSIX path semantics are unchanged except for the pre-existing UNC policy.
    """
    candidate = path.expanduser()
    if os.name == "nt":
        _validate_windows_path_form(candidate, what=what)
    locality_candidate = candidate
    if os.name == "nt" and require_local and not candidate.is_absolute():
        locality_candidate = Path(os.path.abspath(candidate))
    if require_local and is_remote_path(locality_candidate):
        raise PathSecurityError(f"{what} uses a UNC or mapped network-drive path")
    if os.name == "nt" and require_local:
        normalized = os.fspath(locality_candidate).replace("/", "\\")
        drive_root = _windows_drive_root(normalized)
        drive_type = _windows_drive_type(drive_root) if drive_root is not None else None
        if drive_type not in _CONFIRMED_LOCAL_DRIVE_TYPES:
            raise PathSecurityError(f"{what} is not on a confirmed local Windows filesystem")
    if os.name == "nt" and reject_reparse:
        _reject_windows_reparse_points(candidate, what=what)
    return candidate


def is_remote_path(path: Path) -> bool:
    """Detect path forms that can trigger network filesystem traffic.

    UNC/device paths are rejected lexically, before any stat/resolve call.
    Local extended drive paths (``\\?\\C:\\...``) are distinguished from
    extended UNC paths. On Windows, mapped drives use ``GetDriveTypeW``.
    """
    raw = os.fspath(path)
    normalized = raw.replace("/", "\\")
    upper = normalized.upper()
    if upper.startswith(f"{_WINDOWS_EXTENDED_PREFIX.upper()}UNC\\"):
        return True
    if upper.startswith(_WINDOWS_DEVICE_PREFIX.upper()):
        return True
    if upper.startswith(_WINDOWS_EXTENDED_PREFIX.upper()):
        drive_root = _windows_drive_root(normalized)
        if drive_root is None:
            return True
        return os.name == "nt" and _windows_drive_type(drive_root) == _DRIVE_REMOTE
    if normalized.startswith("\\\\"):
        return True
    if os.name == "nt":
        drive_root = _windows_drive_root(normalized)
        if drive_root is not None:
            with contextlib.suppress(AttributeError, OSError):
                return _windows_drive_type(drive_root) == _DRIVE_REMOTE
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
    checked_root = validate_path_before_access(root, what=f"{what} root")
    checked_path = validate_path_before_access(path, what=what)
    resolved = checked_path.resolve(strict=False)
    if not is_within(resolved, checked_root):
        raise PathSecurityError(f"{what} escapes its allowed directory")
    return resolved
