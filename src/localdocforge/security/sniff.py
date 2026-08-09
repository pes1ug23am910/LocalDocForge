"""Content-type detection from signatures, with a strict Markdown text boundary."""

from __future__ import annotations

import codecs
from pathlib import Path

_HEADER_BYTES = 4096


class ContentTypeError(Exception):
    """The file's actual content does not match what the operation requires."""


def _sniff(header: bytes) -> str | None:
    if header.startswith(b"%PDF-"):
        return "application/pdf"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    if header.startswith(b"BM"):
        return "image/bmp"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "image/webp"
    if header.startswith(b"GIF8"):
        return "image/gif"
    if len(header) > 11 and header[4:8] == b"ftyp":
        brand = header[8:12]
        if brand in (b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1"):
            return "image/heif"
    if header.startswith(b"PK\x03\x04"):
        # ZIP container: DOCX/PPTX/XLSX/ODF/EPUB/plain ZIP. Refined later by
        # the Office pipeline; callers must not treat this as a safe default.
        return "application/zip"
    if header.startswith(b"{\\rtf"):
        return "application/rtf"
    return None


def detect_media_type(path: Path, *, max_text_bytes: int | None = None) -> str | None:
    """Detect the media type of ``path`` from its leading bytes.

    A PDF header is also accepted within the first 1024 bytes (the PDF spec
    tolerates a preamble), but only when the file extension already claims PDF —
    otherwise a polyglot could smuggle a PDF body under an innocuous type.
    Markdown has no trustworthy magic bytes, so only ``.md``/``.markdown`` files
    that pass a full strict-UTF-8 decode and contain no NUL are classified as
    ``text/markdown``.
    """
    if max_text_bytes is not None and max_text_bytes < 0:
        raise ValueError("max_text_bytes cannot be negative")
    with path.open("rb") as stream:
        header = stream.read(_HEADER_BYTES)
    detected = _sniff(header)
    if detected is None and path.suffix.lower() == ".pdf":
        offset = header[:1024].find(b"%PDF-")
        if offset > 0:
            return "application/pdf"
    if detected is None and path.suffix.lower() in {".md", ".markdown"}:
        decoder = codecs.getincrementaldecoder("utf-8-sig")(errors="strict")
        total = 0
        try:
            with path.open("rb") as stream:
                while True:
                    chunk_size = 64 * 1024
                    if max_text_bytes is not None:
                        chunk_size = min(chunk_size, max_text_bytes - total + 1)
                    chunk = stream.read(chunk_size)
                    if not chunk:
                        break
                    total += len(chunk)
                    if max_text_bytes is not None and total > max_text_bytes:
                        return None
                    if b"\x00" in chunk:
                        return None
                    decoder.decode(chunk)
                decoder.decode(b"", final=True)
        except UnicodeDecodeError:
            return None
        return "text/markdown"
    return detected


def require_media_type(
    path: Path,
    *expected: str,
    max_text_bytes: int | None = None,
) -> str:
    """Return the detected media type or raise with a clear, safe message."""
    if not path.is_file():
        raise ContentTypeError(f"Not a file: {path.name}")
    detected = detect_media_type(path, max_text_bytes=max_text_bytes)
    if detected is None:
        raise ContentTypeError(
            f"Could not identify the content of {path.name!r}; "
            f"expected {', '.join(expected)}. The file was not processed."
        )
    if detected not in expected:
        raise ContentTypeError(
            f"{path.name!r} contains {detected}, but this operation requires "
            f"{', '.join(expected)}. Rename tricks are ignored; the operation's "
            f"signature/text boundary decides."
        )
    return detected
