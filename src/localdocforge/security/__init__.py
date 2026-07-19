"""Security primitives: path containment, filename hygiene, content sniffing, limits."""

from localdocforge.security.filenames import sanitize_filename
from localdocforge.security.paths import PathSecurityError, ensure_contained, is_within
from localdocforge.security.sniff import (
    ContentTypeError,
    detect_media_type,
    require_media_type,
)

__all__ = [
    "ContentTypeError",
    "PathSecurityError",
    "detect_media_type",
    "ensure_contained",
    "is_within",
    "require_media_type",
    "sanitize_filename",
]
