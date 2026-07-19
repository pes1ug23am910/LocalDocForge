"""Filename sanitization for attachment extraction and generated outputs.

Treats every incoming name as hostile: strips directory components, traversal
sequences, control characters, reserved Windows device names, and characters
that are unsafe on any supported platform. The result is always a plain
filename that cannot navigate the filesystem.
"""

from __future__ import annotations

import re
import unicodedata

_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_WINDOWS_RESERVED = re.compile(
    r"^(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(\.|$)",
    re.IGNORECASE,
)
_MAX_LENGTH = 150  # generous but far below any platform limit, leaves room for suffixes


def sanitize_filename(name: str, *, fallback: str = "file") -> str:
    """Reduce an untrusted string to a safe, plain filename."""
    normalized = unicodedata.normalize("NFC", name)
    # Keep only the last path component, whichever separator convention was used.
    for separator in ("/", "\\"):
        normalized = normalized.rsplit(separator, 1)[-1]
    cleaned = _UNSAFE_CHARS.sub("_", normalized)
    cleaned = cleaned.strip().strip(".")
    if _WINDOWS_RESERVED.match(cleaned):
        cleaned = f"_{cleaned}"
    if len(cleaned) > _MAX_LENGTH:
        stem, dot, suffix = cleaned.rpartition(".")
        if dot and 0 < len(suffix) <= 10:
            keep = _MAX_LENGTH - len(suffix) - 1
            cleaned = f"{stem[:keep]}.{suffix}"
        else:
            cleaned = cleaned[:_MAX_LENGTH]
    if not cleaned or set(cleaned) <= {".", "_", " "}:
        return fallback
    return cleaned
