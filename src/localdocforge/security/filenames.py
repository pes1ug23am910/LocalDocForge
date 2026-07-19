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
    r"^(CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|CLOCK\$|COM[1-9¹²³]|LPT[1-9¹²³])(\.|$)",
    re.IGNORECASE,
)
_MAX_LENGTH = 150  # generous but far below any platform limit, leaves room for suffixes
_MAX_UTF8_BYTES = 180  # also keeps astral names comfortably below Windows UTF-16 limits


def _truncate_utf8(value: str, max_bytes: int) -> str:
    return value.encode("utf-8", errors="ignore")[:max_bytes].decode("utf-8", errors="ignore")


def sanitize_filename(name: str, *, fallback: str = "file") -> str:
    """Reduce an untrusted string to a safe, plain filename."""
    normalized = unicodedata.normalize("NFC", name)
    # Keep only the last path component, whichever separator convention was used.
    for separator in ("/", "\\"):
        normalized = normalized.rsplit(separator, 1)[-1]
    normalized = "".join(
        "_" if unicodedata.category(char) in {"Cc", "Cf", "Cs"} else char
        for char in normalized
    )
    cleaned = _UNSAFE_CHARS.sub("_", normalized)
    cleaned = cleaned.strip().strip(".")
    if _WINDOWS_RESERVED.match(cleaned):
        cleaned = f"_{cleaned}"
    # Enforce the byte bound first. Truncating astral Unicode by code points
    # first could discard a short extension before the byte-aware branch had
    # a chance to preserve it.
    if len(cleaned.encode("utf-8", errors="ignore")) > _MAX_UTF8_BYTES:
        stem, dot, suffix = cleaned.rpartition(".")
        suffix_bytes = len(suffix.encode("utf-8", errors="ignore"))
        if dot and 0 < len(suffix) <= 10 and suffix_bytes + 1 < _MAX_UTF8_BYTES:
            stem_budget = _MAX_UTF8_BYTES - suffix_bytes - 1
            cleaned = f"{_truncate_utf8(stem, stem_budget)}.{suffix}"
        else:
            cleaned = _truncate_utf8(cleaned, _MAX_UTF8_BYTES)
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
