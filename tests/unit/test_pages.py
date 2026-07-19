"""Page-range grammar: syntax validation and resolution against page counts."""

import pytest

from localdocforge.domain.pages import PageRange, PageRangeError


class TestParsing:
    @pytest.mark.parametrize(
        "spec",
        ["1", "1-5", "1,3,7", "1-5,9,12-end", "odd", "even", "reverse", "last", "last-5",
         "all", "end", "9-2", " 1 , 3 ", "1-END", "LAST"],
    )
    def test_valid_specs_parse(self, spec):
        assert PageRange(spec=spec).spec == spec

    @pytest.mark.parametrize(
        "spec",
        ["", ",", "1,,3", "0", "0-5", "abc", "1-", "-5", "last-0", "1--3", "1-5-9",
         "first", "1;3", "last-", "end-3"],
    )
    def test_invalid_specs_rejected(self, spec):
        with pytest.raises((PageRangeError, ValueError)):
            PageRange(spec=spec)


class TestResolution:
    def test_single_and_list(self):
        assert PageRange(spec="1").resolve(10) == (1,)
        assert PageRange(spec="1,3,7").resolve(10) == (1, 3, 7)

    def test_ascending_and_descending_ranges(self):
        assert PageRange(spec="1-5").resolve(10) == (1, 2, 3, 4, 5)
        assert PageRange(spec="5-1").resolve(10) == (5, 4, 3, 2, 1)

    def test_end_keyword(self):
        assert PageRange(spec="12-end").resolve(14) == (12, 13, 14)
        assert PageRange(spec="end").resolve(3) == (3,)

    def test_compound(self):
        assert PageRange(spec="1-5,9,12-end").resolve(14) == (1, 2, 3, 4, 5, 9, 12, 13, 14)

    def test_odd_even(self):
        assert PageRange(spec="odd").resolve(6) == (1, 3, 5)
        assert PageRange(spec="even").resolve(6) == (2, 4, 6)
        assert PageRange(spec="odd").resolve(1) == (1,)

    def test_all_and_reverse(self):
        assert PageRange(spec="all").resolve(4) == (1, 2, 3, 4)
        assert PageRange(spec="reverse").resolve(4) == (4, 3, 2, 1)

    def test_last_and_last_n(self):
        assert PageRange(spec="last").resolve(9) == (9,)
        assert PageRange(spec="last-5").resolve(9) == (5, 6, 7, 8, 9)
        assert PageRange(spec="last-1").resolve(9) == (9,)

    def test_duplicates_preserved(self):
        assert PageRange(spec="2,2,1").resolve(3) == (2, 2, 1)

    def test_out_of_bounds(self):
        with pytest.raises(PageRangeError, match="out of bounds"):
            PageRange(spec="11").resolve(10)
        with pytest.raises(PageRangeError, match="out of bounds"):
            PageRange(spec="8-12").resolve(10)
        with pytest.raises(PageRangeError, match="last-5"):
            PageRange(spec="last-5").resolve(3)

    def test_start_beyond_end_keyword_bounds(self):
        with pytest.raises(PageRangeError):
            PageRange(spec="12-end").resolve(5)

    def test_empty_document(self):
        with pytest.raises(PageRangeError, match="no pages"):
            PageRange(spec="all").resolve(0)

    def test_default_is_all(self):
        assert PageRange().resolve(3) == (1, 2, 3)
