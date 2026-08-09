"""Magic-byte detection: extensions never decide, content does."""

import pytest

from localdocforge.security.sniff import ContentTypeError, detect_media_type, require_media_type

PNG_HEADER = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG_HEADER = b"\xff\xd8\xff\xe0" + b"\x00" * 32
PDF_HEADER = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n"
JP2_HEADER = b"\x00\x00\x00\x0cjP  \r\n\x87\n\x00\x00\x00\x14ftypjp2 "


class TestDetect:
    @pytest.mark.parametrize(
        "content,expected",
        [
            (PDF_HEADER, "application/pdf"),
            (PNG_HEADER, "image/png"),
            (JPEG_HEADER, "image/jpeg"),
            (b"II*\x00rest", "image/tiff"),
            (b"MM\x00*rest", "image/tiff"),
            (b"BM_bitmap", "image/bmp"),
            (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"),
            (b"PK\x03\x04office", "application/zip"),
            (b"{\\rtf1 hello}", "application/rtf"),
            # JPEG2000 is deliberately not an enabled input type. Keep it at
            # the sniff boundary so Pillow's bundled OpenJPEG decoder is not
            # reachable through images-to-PDF.
            (JP2_HEADER, None),
            (b"plain text, nothing else", None),
            (b"", None),
        ],
    )
    def test_signatures(self, tmp_path, content, expected):
        f = tmp_path / "sample.bin"
        f.write_bytes(content)
        assert detect_media_type(f) == expected

    def test_pdf_with_preamble_only_for_pdf_extension(self, tmp_path):
        content = b"\xef\xbb\xbfjunk preamble\n%PDF-1.4\n"
        as_pdf = tmp_path / "doc.pdf"
        as_pdf.write_bytes(content)
        assert detect_media_type(as_pdf) == "application/pdf"
        as_other = tmp_path / "doc.txt"
        as_other.write_bytes(content)
        assert detect_media_type(as_other) is None

    @pytest.mark.parametrize("suffix", [".md", ".markdown", ".MD"])
    def test_markdown_requires_a_markdown_extension_and_full_valid_utf8(self, tmp_path, suffix):
        markdown = tmp_path / f"report{suffix}"
        markdown.write_text("# Résumé 履歴書\n\nStrict UTF-8.\n", encoding="utf-8-sig")

        assert detect_media_type(markdown) == "text/markdown"
        assert require_media_type(markdown, "text/markdown") == "text/markdown"

        same_bytes = tmp_path / "report.txt"
        same_bytes.write_bytes(markdown.read_bytes())
        assert detect_media_type(same_bytes) is None

    @pytest.mark.parametrize(
        "content",
        [
            b"# valid prefix\n" + b"a" * 5000 + b"\xff",
            b"# valid prefix\n" + b"a" * 5000 + b"\x00binary",
        ],
    )
    def test_markdown_detection_checks_beyond_the_header(self, tmp_path, content):
        markdown = tmp_path / "hostile.md"
        markdown.write_bytes(content)

        assert detect_media_type(markdown) is None
        with pytest.raises(ContentTypeError, match="Could not identify"):
            require_media_type(markdown, "text/markdown")

    def test_signature_still_outranks_a_markdown_extension(self, tmp_path):
        disguised = tmp_path / "disguised.md"
        disguised.write_bytes(PNG_HEADER)

        assert detect_media_type(disguised) == "image/png"
        with pytest.raises(ContentTypeError, match="image/png"):
            require_media_type(disguised, "text/markdown")

    def test_markdown_detection_honours_a_bounded_full_text_scan(self, tmp_path):
        markdown = tmp_path / "bounded.md"
        markdown.write_bytes(b"# heading\n" + b"x" * 128)

        assert detect_media_type(markdown, max_text_bytes=16) is None
        with pytest.raises(ContentTypeError, match="Could not identify"):
            require_media_type(markdown, "text/markdown", max_text_bytes=16)
        assert detect_media_type(markdown, max_text_bytes=markdown.stat().st_size) == (
            "text/markdown"
        )


class TestRequire:
    def test_wrong_content_rejected_despite_extension(self, tmp_path):
        fake = tmp_path / "fake.pdf"
        fake.write_bytes(PNG_HEADER)
        with pytest.raises(ContentTypeError, match="image/png"):
            require_media_type(fake, "application/pdf")

    def test_matching_content_accepted(self, tmp_path):
        real = tmp_path / "real.dat"  # wrong extension is fine, content decides
        real.write_bytes(PDF_HEADER)
        assert require_media_type(real, "application/pdf") == "application/pdf"

    def test_unknown_content_rejected(self, tmp_path):
        blob = tmp_path / "mystery.pdf"
        blob.write_bytes(b"not a document at all")
        with pytest.raises(ContentTypeError, match="Could not identify"):
            require_media_type(blob, "application/pdf")

    def test_missing_file(self, tmp_path):
        with pytest.raises(ContentTypeError, match="Not a file"):
            require_media_type(tmp_path / "absent.pdf", "application/pdf")
