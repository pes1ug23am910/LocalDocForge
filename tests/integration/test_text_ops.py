"""Integration tests for deterministic, honest PDF text extraction."""

from __future__ import annotations

import json
import re
import statistics
import unicodedata
from pathlib import Path

import pytest

from localdocforge.config.settings import Settings
from localdocforge.domain.models import ConversionReport, ReportStatus, ResourceLimits
from localdocforge.domain.pages import PageRange
from localdocforge.operations import text as text_ops
from localdocforge.operations.organize import inspect_pdf
from localdocforge.operations.text import PdfToMdOptions, pdf_to_md
from localdocforge.pipelines.runner import PipelineError

WARNING_CODES = frozenset(
    {
        "no-text-layer",
        "headings-inferred",
        "reading-order-uncertain",
        "tables-flattened",
    }
)
COVERAGE_KEYS = {
    "pages_total",
    "pages_with_text",
    "pages_with_text_layer",
    "char_count_min",
    "char_count_median",
    "char_count_max",
    "per_page",
}
PER_PAGE_KEYS = {"page", "char_count", "has_text_layer", "warning_codes"}
JSONL_KEYS = ["page", "text", "char_count", "has_text_layer"]


def _settings(root: Path, *, limits: ResourceLimits | None = None) -> Settings:
    return Settings(jobs_root=root / "jobs", limits=limits or ResourceLimits())


def _options(root: Path, **updates: object) -> PdfToMdOptions:
    values: dict[str, object] = {"settings": _settings(root)}
    values.update(updates)
    return PdfToMdOptions(**values)


def _warning_codes(report: ConversionReport) -> set[str]:
    return {warning.code for warning in report.fidelity_warnings}


def _anchor_pages(text: str) -> list[int]:
    return [int(page) for page in re.findall(r"(?m)^<!-- ldf:page (\d+) -->$", text)]


def _txt_anchor_pages(text: str) -> list[int]:
    return [int(page) for page in re.findall(r"(?m)^--- ldf:page (\d+) ---$", text)]


def _assert_coverage_shape(report: ConversionReport) -> dict[str, object]:
    coverage = report.details["coverage"]
    assert isinstance(coverage, dict)
    assert set(coverage) == COVERAGE_KEYS
    per_page = coverage["per_page"]
    assert isinstance(per_page, list)
    assert len(per_page) == coverage["pages_total"]
    assert all(isinstance(item, dict) and set(item) == PER_PAGE_KEYS for item in per_page)

    counts = [item["char_count"] for item in per_page]
    assert all(isinstance(count, int) and count >= 0 for count in counts)
    assert coverage["pages_with_text"] == sum(count > 0 for count in counts)
    assert coverage["pages_with_text_layer"] == sum(
        bool(item["has_text_layer"]) for item in per_page
    )
    assert coverage["char_count_min"] == min(counts)
    assert coverage["char_count_median"] == statistics.median(counts)
    assert coverage["char_count_max"] == max(counts)
    for item in per_page:
        assert isinstance(item["page"], int) and item["page"] >= 1
        assert isinstance(item["has_text_layer"], bool)
        assert isinstance(item["warning_codes"], list)
        assert set(item["warning_codes"]) <= WARNING_CODES
    return coverage


def test_markdown_is_structured_anchored_private_and_deterministic(
    fixtures_dir: Path, out_dir: Path, tmp_path: Path
) -> None:
    source = fixtures_dir / "simple-3page.pdf"
    first = out_dir / "first.md"
    second = out_dir / "second.md"

    first_report = pdf_to_md(source, first, options=_options(tmp_path))
    second_report = pdf_to_md(source, second, options=_options(tmp_path))

    assert first_report.status is ReportStatus.SUCCESS
    assert first.read_bytes() == second.read_bytes()
    text = first.read_bytes().decode("utf-8", errors="strict")
    assert _anchor_pages(text) == [1, 2, 3]
    assert re.search(r"(?m)^#{1,6} Alpha Section 1$", text)
    assert [int(page) for page in re.findall(r"MARKER-ALPHA-PAGE-(\d)", text)] == [1, 2, 3]
    assert "headings-inferred" in _warning_codes(first_report)

    coverage = _assert_coverage_shape(first_report)
    assert coverage["pages_total"] == 3
    assert coverage["pages_with_text"] == 3
    assert coverage["pages_with_text_layer"] == 3
    assert coverage == second_report.details["coverage"]
    assert [
        (warning.code, warning.page, warning.message)
        for warning in first_report.fidelity_warnings
    ] == [
        (warning.code, warning.page, warning.message)
        for warning in second_report.fidelity_warnings
    ]

    # Reports carry only coverage metrics and warning metadata, never document text.
    serialized_report = first_report.model_dump_json()
    assert "MARKER-ALPHA-PAGE-1" not in serialized_report
    assert "This is synthetic fixture text" not in serialized_report
    assert '"text":' not in serialized_report

    assert first_report.validation is not None and first_report.validation.passed
    check_names = {check.name.lower() for check in first_report.validation.checks}
    assert any("utf" in name for name in check_names)
    assert any("anchor" in name for name in check_names)
    assert any("coverage" in name for name in check_names)


def test_page_selection_preserves_reverse_order_and_duplicates(
    fixtures_dir: Path, out_dir: Path, tmp_path: Path
) -> None:
    output = out_dir / "selected.md"
    report = pdf_to_md(
        fixtures_dir / "simple-3page.pdf",
        output,
        options=_options(tmp_path, pages=PageRange(spec="reverse,2")),
    )

    text = output.read_text(encoding="utf-8")
    assert _anchor_pages(text) == [3, 2, 1, 2]
    assert [int(page) for page in re.findall(r"MARKER-ALPHA-PAGE-(\d)", text)] == [3, 2, 1, 2]
    coverage = _assert_coverage_shape(report)
    assert coverage["pages_total"] == 4
    assert [item["page"] for item in coverage["per_page"]] == [3, 2, 1, 2]
    assert report.output_page_count == 4


def test_jsonl_has_one_exact_record_per_selected_occurrence(
    fixtures_dir: Path, out_dir: Path, tmp_path: Path
) -> None:
    output = out_dir / "selected.jsonl"
    report = pdf_to_md(
        fixtures_dir / "simple-3page.pdf",
        output,
        options=_options(
            tmp_path,
            output_format="jsonl",
            pages=PageRange(spec="3,1,1"),
        ),
    )

    raw = output.read_bytes().decode("utf-8", errors="strict")
    records = [json.loads(line) for line in raw.splitlines()]
    assert [list(record) for record in records] == [JSONL_KEYS, JSONL_KEYS, JSONL_KEYS]
    assert [record["page"] for record in records] == [3, 1, 1]
    assert all(record["char_count"] == len(record["text"]) for record in records)
    assert all(record["has_text_layer"] is True for record in records)
    assert all("<!-- ldf:page" not in record["text"] for record in records)
    coverage = _assert_coverage_shape(report)
    assert [item["char_count"] for item in coverage["per_page"]] == [
        record["char_count"] for record in records
    ]


def test_txt_uses_exact_anchors_or_form_feed_separators(
    fixtures_dir: Path, out_dir: Path, tmp_path: Path
) -> None:
    source = fixtures_dir / "simple-3page.pdf"
    anchored = out_dir / "anchored.txt"
    plain = out_dir / "plain.txt"

    pdf_to_md(
        source,
        anchored,
        options=_options(tmp_path, output_format="txt", page_anchors=True),
    )
    pdf_to_md(
        source,
        plain,
        options=_options(tmp_path, output_format="txt", page_anchors=False),
    )

    anchored_text = anchored.read_text(encoding="utf-8")
    assert _txt_anchor_pages(anchored_text) == [1, 2, 3]
    assert "<!-- ldf:page" not in anchored_text
    assert "\f" not in anchored_text

    plain_text = plain.read_text(encoding="utf-8")
    assert "<!-- ldf:page" not in plain_text
    assert "--- ldf:page" not in plain_text
    assert plain_text.count("\f") == 2
    parts = plain_text.split("\f")
    assert len(parts) == 3
    assert all(f"MARKER-ALPHA-PAGE-{page}" in part for page, part in enumerate(parts, 1))


def test_markdown_no_anchors_and_jsonl_anchor_toggle_semantics(
    fixtures_dir: Path, out_dir: Path, tmp_path: Path
) -> None:
    source = fixtures_dir / "simple-3page.pdf"
    markdown = out_dir / "no-anchors.md"
    jsonl_anchored = out_dir / "anchored.jsonl"
    jsonl_unanchored = out_dir / "unanchored.jsonl"

    pdf_to_md(
        source,
        markdown,
        options=_options(tmp_path, output_format="md", page_anchors=False),
    )
    anchored_report = pdf_to_md(
        source,
        jsonl_anchored,
        options=_options(tmp_path, output_format="jsonl", page_anchors=True),
    )
    unanchored_report = pdf_to_md(
        source,
        jsonl_unanchored,
        options=_options(tmp_path, output_format="jsonl", page_anchors=False),
    )

    markdown_text = markdown.read_text(encoding="utf-8")
    assert "<!-- ldf:page" not in markdown_text
    assert [int(page) for page in re.findall(r"MARKER-ALPHA-PAGE-(\d)", markdown_text)] == [
        1,
        2,
        3,
    ]
    assert re.search(r"Page 1 of 3\n{2,}#{1,6} Alpha Section 2", markdown_text)
    assert re.search(r"Page 2 of 3\n{2,}#{1,6} Alpha Section 3", markdown_text)

    assert jsonl_anchored.read_bytes() == jsonl_unanchored.read_bytes()
    assert anchored_report.details["coverage"] == unanchored_report.details["coverage"]


def test_missing_and_whitespace_only_text_layers_are_distinguished(
    fixtures_dir: Path, out_dir: Path, tmp_path: Path
) -> None:
    image_output = out_dir / "image-only.md"
    image_report = pdf_to_md(
        fixtures_dir / "text-image-only.pdf",
        image_output,
        options=_options(tmp_path),
    )
    image_text = image_output.read_text(encoding="utf-8")
    assert _anchor_pages(image_text) == [1]
    image_coverage = _assert_coverage_shape(image_report)
    assert image_coverage["pages_with_text"] == 0
    assert image_coverage["pages_with_text_layer"] == 0
    assert image_coverage["per_page"] == [
        {
            "page": 1,
            "char_count": 0,
            "has_text_layer": False,
            "warning_codes": ["no-text-layer"],
        }
    ]
    no_text_warning = next(
        warning for warning in image_report.fidelity_warnings if warning.code == "no-text-layer"
    )
    assert no_text_warning.page is None
    assert "pdf-to-images --preset llm" in no_text_warning.message

    whitespace_output = out_dir / "whitespace.md"
    whitespace_report = pdf_to_md(
        fixtures_dir / "text-whitespace.pdf",
        whitespace_output,
        options=_options(tmp_path),
    )
    whitespace_coverage = _assert_coverage_shape(whitespace_report)
    assert whitespace_coverage["pages_with_text"] == 0
    assert whitespace_coverage["pages_with_text_layer"] == 1
    assert whitespace_coverage["per_page"][0]["char_count"] == 0
    assert whitespace_coverage["per_page"][0]["has_text_layer"] is True
    assert "no-text-layer" not in _warning_codes(whitespace_report)


@pytest.mark.parametrize(
    ("fixture_name", "expected_code", "content_marker"),
    [
        ("simple-3page.pdf", "headings-inferred", "MARKER-ALPHA-PAGE-1"),
        ("text-two-column.pdf", "reading-order-uncertain", "RIGHT-D"),
        ("text-ruled-table.pdf", "tables-flattened", "Revenue"),
        ("text-rtl.pdf", "reading-order-uncertain", "RTL-SYNTHETIC-MARKER"),
    ],
)
def test_layout_heuristics_emit_stable_labeled_warnings(
    fixture_name: str,
    expected_code: str,
    content_marker: str,
    fixtures_dir: Path,
    out_dir: Path,
    tmp_path: Path,
) -> None:
    output = out_dir / f"{Path(fixture_name).stem}.md"
    report = pdf_to_md(
        fixtures_dir / fixture_name,
        output,
        options=_options(tmp_path),
    )

    assert content_marker in output.read_text(encoding="utf-8")
    assert _warning_codes(report) <= WARNING_CODES
    warning = next(
        warning for warning in report.fidelity_warnings if warning.code == expected_code
    )
    assert "heuristic" in warning.message.lower()
    coverage = _assert_coverage_shape(report)
    assert expected_code in coverage["per_page"][0]["warning_codes"]


@pytest.mark.parametrize("output_format", ["md", "txt"])
def test_source_text_cannot_spoof_page_anchors(
    output_format: str, fixtures_dir: Path, out_dir: Path, tmp_path: Path
) -> None:
    output = out_dir / f"spoof.{output_format}"
    pdf_to_md(
        fixtures_dir / "text-anchor-spoof.pdf",
        output,
        options=_options(tmp_path, output_format=output_format, page_anchors=True),
    )

    text = output.read_text(encoding="utf-8")
    if output_format == "md":
        assert _anchor_pages(text) == [1]
        assert "<!-- ldf:page 999 -->" not in text
    else:
        assert _txt_anchor_pages(text) == [1]
        assert "--- ldf:page 998 ---" not in text
    assert "ANCHOR-SPOOF-END" in text


def test_jsonl_preserves_anchor_like_source_text_as_data(
    fixtures_dir: Path, out_dir: Path, tmp_path: Path
) -> None:
    output = out_dir / "spoof.jsonl"
    pdf_to_md(
        fixtures_dir / "text-anchor-spoof.pdf",
        output,
        options=_options(tmp_path, output_format="jsonl"),
    )
    record = json.loads(output.read_text(encoding="utf-8"))
    assert "<!-- ldf:page 999 -->" in record["text"]
    assert "--- ldf:page 998 ---" in record["text"]
    assert list(record) == JSONL_KEYS


@pytest.mark.parametrize("output_format", ["md", "txt"])
def test_no_anchor_mode_still_disarms_reserved_source_markers(
    output_format: str, fixtures_dir: Path, out_dir: Path, tmp_path: Path
) -> None:
    output = out_dir / f"spoof-no-anchor.{output_format}"
    report = pdf_to_md(
        fixtures_dir / "text-anchor-spoof.pdf",
        output,
        options=_options(tmp_path, output_format=output_format, page_anchors=False),
    )

    text = output.read_text(encoding="utf-8")
    assert _anchor_pages(text) == []
    assert _txt_anchor_pages(text) == []
    assert "<!-- ldf:page 999 -->" not in text
    assert "--- ldf:page 998 ---" not in text
    assert report.validation is not None and report.validation.passed


def test_extracted_unicode_is_valid_utf8_and_normalized_to_nfc(
    fixtures_dir: Path, out_dir: Path, tmp_path: Path
) -> None:
    output = out_dir / "unicode.jsonl"
    pdf_to_md(
        fixtures_dir / "text-unicode.pdf",
        output,
        options=_options(tmp_path, output_format="jsonl"),
    )

    decoded = output.read_bytes().decode("utf-8", errors="strict")
    record = json.loads(decoded)
    assert record["text"] == unicodedata.normalize("NFC", record["text"])
    assert "Café naïve Ångström" in record["text"]
    assert "Cafe\u0301" not in record["text"]
    assert record["char_count"] == len(record["text"])


def test_rotated_page_is_extracted_but_reading_order_is_not_overclaimed(
    fixtures_dir: Path, out_dir: Path, tmp_path: Path
) -> None:
    output = out_dir / "rotated.md"
    report = pdf_to_md(
        fixtures_dir / "rotated-mixed.pdf",
        output,
        options=_options(tmp_path, pages=PageRange(spec="2")),
    )

    assert "MARKER-ALPHA-PAGE-2" in output.read_text(encoding="utf-8")
    assert "reading-order-uncertain" in _warning_codes(report)
    coverage = _assert_coverage_shape(report)
    assert coverage["per_page"][0]["page"] == 2
    assert "reading-order-uncertain" in coverage["per_page"][0]["warning_codes"]


def test_geometry_join_preserves_inline_styles_and_separates_columns(
    fixtures_dir: Path, out_dir: Path, tmp_path: Path
) -> None:
    output = out_dir / "styled.jsonl"
    pdf_to_md(
        fixtures_dir / "text-styled-fragments.pdf",
        output,
        options=_options(tmp_path, output_format="jsonl"),
    )
    extracted = json.loads(output.read_text(encoding="utf-8"))["text"]
    assert "HelloWorld" in extracted
    assert "Hello World" not in extracted
    assert "SAME SAME" in extracted


def test_jsonl_validator_compares_records_with_coverage(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.jsonl"
    candidate.write_text(
        '{"page":1,"text":"abc","char_count":3,"has_text_layer":true}\n',
        encoding="utf-8",
        newline="\n",
    )
    coverage: dict[str, object] = {
        "pages_total": 1,
        "pages_with_text": 1,
        "pages_with_text_layer": 0,
        "char_count_min": 2,
        "char_count_median": 2,
        "char_count_max": 2,
        "per_page": [
            {
                "page": 1,
                "char_count": 2,
                "has_text_layer": False,
                "warning_codes": [],
            }
        ],
    }
    validation = text_ops._text_validator(  # noqa: SLF001 - integrity contract test
        output_format="jsonl",
        page_anchors=False,
        selection=(1,),
        coverage=coverage,
    )(candidate)
    assert not validation.passed
    check = next(item for item in validation.checks if item.name == "jsonl-records-exact")
    assert not check.passed


def test_coverage_validator_rejects_boolean_integer_fields() -> None:
    coverage: dict[str, object] = {
        "pages_total": 1,
        "pages_with_text": 1,
        "pages_with_text_layer": 1,
        "char_count_min": 1,
        "char_count_median": 1,
        "char_count_max": 1,
        "per_page": [
            {
                "page": 1,
                "char_count": 1,
                "has_text_layer": True,
                "warning_codes": [],
            }
        ],
    }
    for key in (
        "pages_total",
        "pages_with_text",
        "pages_with_text_layer",
        "char_count_min",
        "char_count_median",
        "char_count_max",
    ):
        coverage[key] = True
        valid, _ = text_ops._coverage_shape_valid(coverage, (1,))  # noqa: SLF001
        assert not valid
        coverage[key] = 1

    per_page = coverage["per_page"]
    assert isinstance(per_page, list)
    record = per_page[0]
    assert isinstance(record, dict)
    for key in ("page", "char_count"):
        record[key] = True
        valid, _ = text_ops._coverage_shape_valid(coverage, (1,))  # noqa: SLF001
        assert not valid
        record[key] = 1


def test_page_object_scan_treats_terminal_form_depth_as_truncated() -> None:
    import pypdfium2.raw as pdfium_c

    class TerminalForm:
        type = pdfium_c.FPDF_PAGEOBJ_FORM
        level = 14

    class Page:
        @staticmethod
        def get_objects(*, max_depth: int):
            assert max_depth == 15
            yield TerminalForm()

    scan = text_ops._scan_page_objects(  # noqa: SLF001
        Page(),
        analyze_layout=False,
        check_cancelled=None,
    )
    assert scan.truncated
    assert not scan.has_text_object


def test_inspect_zero_page_pdf_has_defined_empty_text_summary(fixtures_dir: Path) -> None:
    info = inspect_pdf(fixtures_dir / "text-zero-page.pdf")
    assert info["page_count"] == 0
    assert info["page_text_stats"] == []
    assert info["text_coverage"] == {
        "pages_total": 0,
        "pages_with_text": 0,
        "pages_with_text_layer": 0,
        "char_count_min": None,
        "char_count_median": None,
        "char_count_max": None,
    }


def test_inspect_text_stats_honor_decompressed_budget(
    fixtures_dir: Path, tmp_path: Path
) -> None:
    with pytest.raises(PipelineError, match="decompressed-text limit"):
        inspect_pdf(
            fixtures_dir / "text-whitespace.pdf",
            settings=_settings(
                tmp_path,
                limits=ResourceLimits(max_decompressed_bytes=0),
            ),
        )


def test_inspect_text_stats_enforce_exact_normalized_byte_budget(
    fixtures_dir: Path, tmp_path: Path
) -> None:
    with pytest.raises(PipelineError, match="37-byte decompressed limit"):
        inspect_pdf(
            fixtures_dir / "text-rtl.pdf",
            settings=_settings(
                tmp_path,
                limits=ResourceLimits(max_decompressed_bytes=37),
            ),
        )


def test_raw_text_preflight_honors_zero_decompressed_budget(
    fixtures_dir: Path, out_dir: Path, tmp_path: Path
) -> None:
    output = out_dir / "whitespace-never.md"
    with pytest.raises(PipelineError, match="decompressed-text limit"):
        pdf_to_md(
            fixtures_dir / "text-whitespace.pdf",
            output,
            options=PdfToMdOptions(
                settings=_settings(
                    tmp_path,
                    limits=ResourceLimits(max_decompressed_bytes=0),
                )
            ),
        )
    assert not output.exists()


def test_unknown_output_format_is_refused_without_publishing(
    fixtures_dir: Path, out_dir: Path, tmp_path: Path
) -> None:
    output = out_dir / "never.html"
    with pytest.raises(PipelineError, match="(?i)format"):
        pdf_to_md(
            fixtures_dir / "simple-3page.pdf",
            output,
            options=_options(tmp_path, output_format="html"),
        )
    assert not output.exists()


def test_encrypted_input_uses_password_without_leaking_it(
    fixtures_dir: Path,
    fixture_password: str,
    out_dir: Path,
    tmp_path: Path,
) -> None:
    output = out_dir / "encrypted.md"
    report = pdf_to_md(
        fixtures_dir / "encrypted.pdf",
        output,
        options=_options(tmp_path, password=fixture_password),
    )
    assert "MARKER-ALPHA-PAGE-1" in output.read_text(encoding="utf-8")
    assert fixture_password not in report.model_dump_json()

    refused = out_dir / "wrong-password.md"
    with pytest.raises(PipelineError):
        pdf_to_md(
            fixtures_dir / "encrypted.pdf",
            refused,
            options=_options(tmp_path, password="wrong-fixture-password"),
        )
    assert not refused.exists()


def test_page_and_output_limits_refuse_without_publishing(
    fixtures_dir: Path,
    fixture_password: str,
    out_dir: Path,
    tmp_path: Path,
) -> None:
    page_limited = out_dir / "page-limit.md"
    with pytest.raises(PipelineError, match="configured limit"):
        pdf_to_md(
            fixtures_dir / "simple-3page.pdf",
            page_limited,
            options=PdfToMdOptions(
                settings=_settings(tmp_path, limits=ResourceLimits(max_pages=2))
            ),
        )
    assert not page_limited.exists()

    encrypted_limited = out_dir / "encrypted-page-limit.md"
    with pytest.raises(PipelineError, match="after opening"):
        pdf_to_md(
            fixtures_dir / "encrypted.pdf",
            encrypted_limited,
            options=PdfToMdOptions(
                pages=PageRange(spec="1"),
                password=fixture_password,
                settings=_settings(tmp_path, limits=ResourceLimits(max_pages=2)),
            ),
        )
    assert not encrypted_limited.exists()

    byte_limited = out_dir / "byte-limit.md"
    with pytest.raises(PipelineError, match="output limit"):
        pdf_to_md(
            fixtures_dir / "simple-3page.pdf",
            byte_limited,
            options=PdfToMdOptions(
                settings=_settings(tmp_path, limits=ResourceLimits(max_output_bytes=1))
            ),
        )
    assert not byte_limited.exists()


def test_thousand_page_jsonl_stays_pagewise_and_report_contains_only_stats(
    fixtures_dir: Path, out_dir: Path, tmp_path: Path
) -> None:
    output = out_dir / "many.jsonl"
    report = pdf_to_md(
        fixtures_dir / "text-many-1000page.pdf",
        output,
        options=_options(tmp_path, output_format="jsonl"),
    )

    raw = output.read_bytes()
    decoded = raw.decode("utf-8", errors="strict")
    lines = decoded.splitlines()
    assert len(lines) == 1000
    first, last = json.loads(lines[0]), json.loads(lines[-1])
    assert first["page"] == 1 and "STREAM-PAGE-0001" in first["text"]
    assert last["page"] == 1000 and "STREAM-PAGE-1000" in last["text"]
    assert len(raw) < 256_000

    coverage = _assert_coverage_shape(report)
    assert coverage["pages_total"] == 1000
    assert coverage["pages_with_text"] == 1000
    assert coverage["pages_with_text_layer"] == 1000
    assert [coverage["per_page"][index]["page"] for index in (0, 499, 999)] == [
        1,
        500,
        1000,
    ]
    serialized_report = report.model_dump_json()
    assert "STREAM-PAGE-" not in serialized_report
    assert len(serialized_report.encode("utf-8")) < 256_000
