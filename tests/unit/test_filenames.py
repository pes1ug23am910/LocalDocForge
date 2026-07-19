"""Filename sanitization against traversal, reserved names, and hostile input."""

import pytest

from localdocforge.security.filenames import sanitize_filename


class TestSanitizeFilename:
    def test_plain_name_untouched(self):
        assert sanitize_filename("report.pdf") == "report.pdf"

    def test_unicode_preserved_and_normalized(self):
        assert sanitize_filename("résumé.pdf") == "résumé.pdf"
        # NFD input normalizes to NFC
        assert sanitize_filename("résumé.pdf") == "résumé.pdf"

    @pytest.mark.parametrize(
        "hostile,expected_tail",
        [
            ("../../etc/passwd", "passwd"),
            ("..\\..\\windows\\system32\\config", "config"),
            ("/absolute/path/name.txt", "name.txt"),
            ("C:\\Users\\victim\\secret.txt", "secret.txt"),
        ],
    )
    def test_traversal_reduced_to_basename(self, hostile, expected_tail):
        result = sanitize_filename(hostile)
        assert result == expected_tail
        assert "/" not in result and "\\" not in result and ".." not in result

    @pytest.mark.parametrize(
        "name",
        [
            "CON",
            "con.txt",
            "PRN.pdf",
            "aux",
            "NUL.tar.gz",
            "CONIN$",
            "CONOUT$.txt",
            "CLOCK$",
            "COM1",
            "lpt9.doc",
        ],
    )
    def test_reserved_device_names_prefixed(self, name):
        result = sanitize_filename(name)
        assert result.startswith("_")

    def test_com10_not_reserved(self):
        assert sanitize_filename("COM10.txt") == "COM10.txt"

    def test_control_chars_and_specials_replaced(self):
        result = sanitize_filename('a<b>c:d"e|f?g*h\x00i.txt')
        assert result == "a_b_c_d_e_f_g_h_i.txt"

    def test_dots_and_spaces_trimmed(self):
        assert sanitize_filename("  name.pdf.  ") == "name.pdf"
        assert sanitize_filename("...") == "file"

    def test_empty_returns_fallback(self):
        assert sanitize_filename("") == "file"
        assert sanitize_filename("///", fallback="attachment") == "attachment"

    def test_long_name_truncated_keeps_extension(self):
        result = sanitize_filename("a" * 500 + ".pdf")
        assert len(result) <= 150
        assert result.endswith(".pdf")
