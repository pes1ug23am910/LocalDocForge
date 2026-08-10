"""OCR policy, validation, failure mapping, and optional real-engine coverage."""

from __future__ import annotations

import hashlib
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import localdocforge.operations.ocr as ocr_ops
from localdocforge.config.settings import Settings
from localdocforge.domain.models import (
    EngineInfo,
    EngineKind,
    JobContext,
    ReportStatus,
    ResourceLimits,
    WarningSeverity,
)
from localdocforge.engines.base import EngineUnavailableError
from localdocforge.engines.registry import EngineRegistry
from localdocforge.operations.ocr import OcrOptions, OcrToolFailure, ocr_pdf
from localdocforge.pipelines.runner import PipelineError
from localdocforge.security.subproc import ToolError, ToolResult, ToolTimeout


def _settings(tmp_path: Path, **limits: int | float | None) -> Settings:
    return Settings(
        jobs_root=tmp_path / "jobs",
        allowed_output_roots=[tmp_path],
        limits=ResourceLimits(**limits),
    )


def _assert_workspace_clean(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs"
    assert not jobs.exists() or list(jobs.iterdir()) == []


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _extract_text(path: Path) -> str:
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(str(path))
    pages: list[str] = []
    try:
        for index in range(len(document)):
            page = document[index]
            try:
                textpage = page.get_textpage()
                try:
                    pages.append(textpage.get_text_bounded(errors="strict"))
                finally:
                    textpage.close()
            finally:
                page.close()
    finally:
        document.close()
    return "\n".join(pages)


def _write_reference_pdf(source: Path, destination: Path, page_count: int) -> None:
    import pikepdf

    with pikepdf.open(source) as original, pikepdf.new() as output:
        for page in original.pages[:page_count]:
            output.pages.append(page)
        output.save(destination)


def _patch_engine_requirements(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = SimpleNamespace(name="ocrmypdf")
    infos = {
        "ocrmypdf": EngineInfo(
            name="ocrmypdf",
            kind=EngineKind.EXECUTABLE,
            available=True,
            version="OCRmyPDF 17.8.1 synthetic",
            path="C:/trusted/ocrmypdf.exe",
            license="MPL-2.0",
        ),
        "tesseract": EngineInfo(
            name="tesseract",
            kind=EngineKind.EXECUTABLE,
            available=True,
            version="tesseract 5.4.0.20240606 synthetic",
            path="C:/trusted/tesseract.exe",
            license="Apache-2.0",
        ),
        "ghostscript": EngineInfo(
            name="ghostscript",
            kind=EngineKind.EXECUTABLE,
            available=True,
            version="10.07.1 synthetic",
            path="C:/trusted/gswin64c.exe",
            license="AGPL-3.0",
        ),
    }
    monkeypatch.setattr(ocr_ops, "_require_ocr_engines", lambda: (engine, infos))
    monkeypatch.setattr(
        ocr_ops,
        "_require_languages",
        lambda _codes, _expected_executable: None,
    )


def _successful_tool(
    reference: Path,
    *,
    page_count: int,
    sidecar_text: str | None = None,
    diagnostic: str = "",
    captured: dict[str, object] | None = None,
):
    def run(tool: str, args: list[str], **kwargs) -> ToolResult:
        if captured is not None:
            captured.update(tool=tool, args=list(args), kwargs=dict(kwargs))
        _write_reference_pdf(reference, Path(args[-1]), page_count)
        resolved_sidecar = sidecar_text
        if resolved_sidecar is None:
            resolved_sidecar = "\f".join(
                f"Alpha Section {page}\r\nMARKER-ALPHA-PAGE-{page}\r\n"
                for page in range(1, page_count + 1)
            )
        Path(args[args.index("--sidecar") + 1]).write_text(
            resolved_sidecar,
            encoding="utf-8",
            newline="",
        )
        return ToolResult(returncode=0, output=diagnostic)

    return run


def _warning_map(report) -> dict[str, object]:
    return {warning.code: warning for warning in report.fidelity_warnings}


def test_missing_ocr_executables_are_reported_together_with_actionable_hints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MissingEngine:
        def __init__(self, name: str, hint: str) -> None:
            self.name = name
            self.hint = hint

        def probe(self) -> EngineInfo:
            return EngineInfo(
                name=self.name,
                kind=EngineKind.EXECUTABLE,
                available=False,
                install_hint=self.hint,
            )

        def supports(self, operation: str) -> bool:
            return self.name == "ocrmypdf" and operation == ocr_ops.OP_OCR

    engines = {
        "ocrmypdf": MissingEngine("ocrmypdf", "install locked OCRmyPDF"),
        "tesseract": MissingEngine("tesseract", "install Tesseract"),
        "ghostscript": MissingEngine("ghostscript", "install Ghostscript"),
    }
    registry = SimpleNamespace(get=engines.get)
    monkeypatch.setattr(ocr_ops, "default_registry", lambda: registry)

    with pytest.raises(EngineUnavailableError) as failure:
        ocr_ops._require_ocr_engines()

    message = str(failure.value)
    for hint in ("install locked OCRmyPDF", "install Tesseract", "install Ghostscript"):
        assert hint in message


def test_ocr_fixtures_distinguish_image_only_mixed_and_born_digital(
    fixtures_dir: Path,
) -> None:
    image_stats, image_coverage = ocr_ops.inspect_page_text_stats(
        fixtures_dir / "ocr-image-only.pdf"
    )
    mixed_stats, mixed_coverage = ocr_ops.inspect_page_text_stats(fixtures_dir / "ocr-mixed.pdf")

    assert image_coverage["pages_total"] == 1
    assert image_coverage["pages_with_text_layer"] == 0
    assert [item["has_text_layer"] for item in image_stats] == [False]
    assert mixed_coverage["pages_total"] == 2
    assert mixed_coverage["pages_with_text_layer"] == 1
    assert [item["has_text_layer"] for item in mixed_stats] == [True, False]
    assert "MARKER-ALPHA-PAGE-1" not in _extract_text(fixtures_dir / "ocr-image-only.pdf")
    assert "MARKER-ALPHA-PAGE-1" in _extract_text(fixtures_dir / "ocr-mixed.pdf")


@pytest.mark.parametrize(
    "fixture_name",
    ["simple-3page.pdf", "ocr-mixed.pdf", "text-whitespace.pdf"],
)
def test_default_policy_refuses_every_existing_text_layer_before_launch(
    fixtures_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fixture_name: str,
) -> None:
    _patch_engine_requirements(monkeypatch)

    def unexpected_tool(*_args, **_kwargs):
        raise AssertionError("OCRmyPDF must not launch for a policy-refused input")

    monkeypatch.setattr(ocr_ops, "run_tool", unexpected_tool)
    output = tmp_path / "never.pdf"
    with pytest.raises(PipelineError, match="already contains a text layer.*--skip-text"):
        ocr_pdf(
            fixtures_dir / fixture_name,
            output,
            options=OcrOptions(settings=_settings(tmp_path)),
        )

    assert not output.exists()
    _assert_workspace_clean(tmp_path)


def test_skip_text_uses_private_bounded_argv_and_warns_about_sidecar_omission(
    fixtures_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_engine_requirements(monkeypatch)
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        ocr_ops,
        "run_tool",
        _successful_tool(
            fixtures_dir / "simple-3page.pdf",
            page_count=2,
            sidecar_text=("[OCR skipped on page(s) 1]\fAlpha Section 2\r\nMARKER-ALPHA-PAGE-2\r\n"),
            diagnostic="page 2 completed",
            captured=captured,
        ),
    )
    output = tmp_path / "ocr.pdf"
    sidecar = tmp_path / "ocr.txt"
    source = fixtures_dir / "ocr-mixed.pdf"
    source_digest = _digest(source)

    report = ocr_pdf(
        source,
        output,
        options=OcrOptions(
            skip_text=True,
            language="eng+deu",
            sidecar=sidecar,
            settings=_settings(tmp_path, max_image_pixels=12_500_000),
        ),
    )

    args = captured["args"]
    kwargs = captured["kwargs"]
    assert isinstance(args, list) and isinstance(kwargs, dict)
    assert captured["tool"] == "ocrmypdf"
    assert args[args.index("--language") + 1] == "eng+deu"
    assert args[args.index("--jobs") + 1] == "1"
    assert args[args.index("--output-type") + 1] == "pdf"
    assert args[args.index("--optimize") + 1] == "0"
    assert args[args.index("--max-image-mpixels") + 1] == "12.5"
    assert "--skip-text" in args and "--force-ocr" not in args
    assert "--invalidate-digital-signatures" not in args
    assert Path(args[-2]).name == "input.pdf"
    assert Path(args[-2]).resolve() != source.resolve()
    assert "ocr-mixed" not in " ".join(args)
    assert kwargs["cwd"] == Path(args[-2]).parent
    assert kwargs["max_output_bytes"] == ocr_ops._MAX_TOOL_OUTPUT_BYTES
    assert kwargs["child_path_tools"] == ("tesseract", "ghostscript")
    assert kwargs["expected_executable"] == "C:/trusted/ocrmypdf.exe"
    assert kwargs["expected_child_executables"] == {
        "tesseract": "C:/trusted/tesseract.exe",
        "ghostscript": "C:/trusted/gswin64c.exe",
    }
    assert 0 < kwargs["timeout"] <= ocr_ops._MAX_OCR_TIMEOUT_SECONDS
    env = kwargs["env_extra"]
    assert isinstance(env, dict)
    assert set(env) == {"HOME", "USERPROFILE", "TEMP", "TMP", "TMPDIR"}
    assert env["USERPROFILE"] == env["HOME"]
    assert all(Path(value).is_relative_to(kwargs["cwd"]) for value in env.values())
    assert sidecar.read_bytes() == (
        b"[OCR skipped on page(s) 1]\fAlpha Section 2\nMARKER-ALPHA-PAGE-2\n"
    )
    assert _digest(source) == source_digest
    warnings = _warning_map(report)
    assert ocr_ops.OCR_TEXT_APPROXIMATE in warnings
    assert ocr_ops.OCR_SIDECAR_OMITS_EXISTING_TEXT in warnings
    assert report.details["mode"] == "skip"
    assert report.details["engine_diagnostics_withheld"] is True
    assert "page 2 completed" not in report.model_dump_json()
    assert report.validation is not None and report.validation.passed
    _assert_workspace_clean(tmp_path)


def test_force_ocr_emits_critical_rasterization_warning(
    fixtures_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_engine_requirements(monkeypatch)
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        ocr_ops,
        "run_tool",
        _successful_tool(
            fixtures_dir / "simple-3page.pdf",
            page_count=3,
            captured=captured,
        ),
    )

    report = ocr_pdf(
        fixtures_dir / "simple-3page.pdf",
        tmp_path / "forced.pdf",
        options=OcrOptions(force_ocr=True, settings=_settings(tmp_path)),
    )

    args = captured["args"]
    assert isinstance(args, list)
    assert "--force-ocr" in args and "--skip-text" not in args
    warning = _warning_map(report)[ocr_ops.OCR_FORCE_RASTERIZED]
    assert warning.severity is WarningSeverity.CRITICAL
    assert ocr_ops.OCR_TEXT_APPROXIMATE in _warning_map(report)
    assert report.details["mode"] == "force"


def test_mutually_exclusive_modes_fail_before_engine_probe(
    fixtures_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ocr_ops,
        "_require_ocr_engines",
        lambda: (_ for _ in ()).throw(AssertionError("engine probe must not run")),
    )
    with pytest.raises(PipelineError, match="mutually exclusive"):
        ocr_pdf(
            fixtures_dir / "ocr-image-only.pdf",
            tmp_path / "never.pdf",
            options=OcrOptions(
                skip_text=True,
                force_ocr=True,
                settings=_settings(tmp_path),
            ),
        )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("eng", ("eng",)),
        (" eng+deu ", ("eng", "deu")),
        ("script/Latin", ("script/Latin",)),
    ],
)
def test_language_argument_accepts_bounded_tesseract_codes(
    value: str, expected: tuple[str, ...]
) -> None:
    assert ocr_ops._language_codes(value) == expected


@pytest.mark.parametrize(
    "value",
    ["", "eng+", "+eng", "eng++deu", "../eng", "eng;calc", "eng deu", "a" * 257],
)
def test_language_argument_rejects_empty_injection_and_overlong_values(value: str) -> None:
    with pytest.raises(PipelineError, match="Tesseract codes"):
        ocr_ops._language_codes(value)


def test_missing_language_pack_is_actionable_and_prevents_ocr_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ocr_ops,
        "_installed_tesseract_languages",
        lambda _expected_executable: frozenset({"eng", "osd"}),
    )
    with pytest.raises(EngineUnavailableError) as failure:
        ocr_ops._require_languages(("eng", "deu", "fra"), "C:/trusted/tesseract.exe")

    message = str(failure.value)
    assert "language pack(s) missing: deu, fra" in message
    assert "tessdata" in message


def test_tesseract_language_inventory_ignores_header_and_noise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ocr_ops,
        "run_tool",
        lambda *_args, **_kwargs: ToolResult(
            returncode=0,
            output="List of available languages in C:\\tessdata (3):\neng\ndeu\nscript/Latin\n",
        ),
    )
    assert ocr_ops._installed_tesseract_languages("C:/trusted/tesseract.exe") == frozenset(
        {"eng", "deu", "script/Latin"}
    )


@pytest.mark.parametrize("configured", [None, 1_200.0])
def test_remaining_timeout_applies_elapsed_time_to_absolute_ocr_safety_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    configured: float | None,
) -> None:
    now = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz is UTC
            return now

    monkeypatch.setattr(ocr_ops, "datetime", FrozenDateTime)
    context = JobContext(
        job_id="bounded-ocr",
        workspace=tmp_path,
        limits=ResourceLimits(timeout_seconds=configured),
        started_at=now - timedelta(seconds=125),
    )

    assert ocr_ops._remaining_timeout(context) == 775.0


def test_skipped_page_sidecar_markers_are_not_semantic_tokens(tmp_path: Path) -> None:
    sidecar = tmp_path / "sidecar.txt"
    sidecar.write_text(
        "[skipped page]\f[OCR skipped on page(s) 2]\fAlpha Section 3\n",
        encoding="utf-8",
    )

    assert ocr_ops._expected_ocr_tokens(sidecar) == ("alpha", "section", "3")


def test_skipped_page_text_line_inside_recognized_page_is_not_a_failure_marker(
    tmp_path: Path,
) -> None:
    sidecar = tmp_path / "literal-marker-text.txt"
    sidecar.write_text("[skipped page]\nAlpha\n", encoding="utf-8", newline="")

    sample = ocr_ops._sample_ocr_sidecar(sidecar, expected_pages=1)

    assert sample.failed_page_markers == 0
    assert sample.has_non_marker_content
    assert sample.tokens == ("skipped", "page", "alpha")


@pytest.mark.parametrize("record", [" [skipped page] ", "[SKIPPED PAGE]"])
def test_noncanonical_skip_sentinel_is_treated_as_ocr_content(
    tmp_path: Path,
    record: str,
) -> None:
    sidecar = tmp_path / "noncanonical-marker.txt"
    sidecar.write_text(record, encoding="utf-8", newline="")

    sample = ocr_ops._sample_ocr_sidecar(sidecar, expected_pages=1)

    assert sample.failed_page_markers == 0
    assert sample.has_non_marker_content
    assert sample.tokens == ("skipped", "page")


def test_streaming_sidecar_scan_counts_form_feed_markers_beyond_token_sample(
    tmp_path: Path,
) -> None:
    sidecar = tmp_path / "long-sidecar.txt"
    sidecar.write_text(
        "Alpha\n"
        + "x" * (ocr_ops._EXPECTED_TOKEN_READ_CHARS - len("Alpha\n"))
        + "\f[skipped page]\f[skipped page]",
        encoding="utf-8",
        newline="",
    )
    checkpoints = 0

    def checkpoint() -> None:
        nonlocal checkpoints
        checkpoints += 1

    sample = ocr_ops._sample_ocr_sidecar(sidecar, check_cancelled=checkpoint)

    assert sample.tokens == ("alpha",)
    assert sample.failed_page_markers == 2
    assert checkpoints > 1


def test_token_sample_cutoff_drops_partial_skip_sentinel(tmp_path: Path) -> None:
    sidecar = tmp_path / "straddled-marker.txt"
    prefix = "Alpha!\f"
    marker_prefix = "[skip"
    padding_chars = ocr_ops._EXPECTED_TOKEN_READ_CHARS - len(prefix) - len(marker_prefix)
    sidecar.write_text(
        prefix + "x" * (padding_chars - 1) + "\f" + marker_prefix + "ped page]\fBeta\n",
        encoding="utf-8",
        newline="",
    )

    sample = ocr_ops._sample_ocr_sidecar(sidecar)

    assert sample.failed_page_markers == 1
    assert sample.tokens == ("alpha",)


def test_truncated_non_marker_record_preserves_complete_tokens(tmp_path: Path) -> None:
    sidecar = tmp_path / "long-single-record.txt"
    sidecar.write_text(
        "Alpha " * (ocr_ops._EXPECTED_TOKEN_READ_CHARS // len("Alpha ") + 2),
        encoding="utf-8",
        newline="",
    )

    sample = ocr_ops._sample_ocr_sidecar(sidecar)

    assert sample.failed_page_markers == 0
    assert sample.tokens == ("alpha",)
    assert sample.has_non_marker_content


@pytest.mark.parametrize(
    "record",
    [
        "[OCR skipped on page",
        "[OCR skipped on pageant]",
        "[OCR skipped on page(s) 0]",
        "[OCR skipped on page(s) 9-2]",
    ],
)
def test_malformed_ocr_skip_records_are_nonblank_content(
    tmp_path: Path,
    record: str,
) -> None:
    sidecar = tmp_path / "malformed-skip-marker.txt"
    sidecar.write_text(record, encoding="utf-8", newline="")

    sample = ocr_ops._sample_ocr_sidecar(sidecar)

    assert sample.has_non_marker_content
    assert sample.tokens


@pytest.mark.parametrize(
    "record",
    ["[OCR skipped on page(s) 2]", "[OCR skipped on page(s) 2-19]"],
)
def test_exact_ocr_skip_records_are_internal_markers(tmp_path: Path, record: str) -> None:
    sidecar = tmp_path / "exact-skip-marker.txt"
    sidecar.write_text("\f" + record, encoding="utf-8", newline="")

    marker_range = ocr_ops._ocr_skipped_page_range(record)
    assert marker_range is not None
    sample = ocr_ops._sample_ocr_sidecar(
        sidecar,
        expected_pages=marker_range[1],
        ineligible_pages=frozenset(range(marker_range[0], marker_range[1] + 1)),
    )

    assert not sample.has_non_marker_content
    assert sample.tokens == ()
    assert sample.failed_page_markers == 0


def test_sidecar_page_topology_requires_every_input_page_record(tmp_path: Path) -> None:
    sidecar = tmp_path / "missing-page-record.txt"
    sidecar.write_text("", encoding="utf-8", newline="")

    with pytest.raises(PipelineError, match="page topology"):
        ocr_ops._sample_ocr_sidecar(sidecar, expected_pages=2)


def test_explicit_skip_range_must_begin_at_current_sidecar_page(tmp_path: Path) -> None:
    sidecar = tmp_path / "discontinuous-range.txt"
    sidecar.write_text(
        "[OCR skipped on page(s) 2]",
        encoding="utf-8",
        newline="",
    )

    with pytest.raises(PipelineError, match="discontinuous page range"):
        ocr_ops._sample_ocr_sidecar(sidecar, expected_pages=2)


def test_overlarge_internal_skip_range_is_rejected(tmp_path: Path) -> None:
    sidecar = tmp_path / "overlarge-range.txt"
    sidecar.write_text(
        "[OCR skipped on page(s) 1-10001]",
        encoding="utf-8",
        newline="",
    )

    with pytest.raises(PipelineError, match="invalid or discontinuous page range"):
        ocr_ops._sample_ocr_sidecar(sidecar, expected_pages=10001)


def test_generic_skip_marker_on_skip_text_ineligible_page_is_not_counted(
    tmp_path: Path,
) -> None:
    sidecar = tmp_path / "ineligible-generic-skip.txt"
    sidecar.write_text("[skipped page]\fAlpha", encoding="utf-8", newline="")

    sample = ocr_ops._sample_ocr_sidecar(
        sidecar,
        expected_pages=2,
        ineligible_pages=frozenset({1}),
    )

    assert sample.failed_page_markers == 0
    assert sample.tokens == ("alpha",)


def test_ocr_skip_range_counts_only_ldf_eligible_pages(tmp_path: Path) -> None:
    sidecar = tmp_path / "mixed-eligibility-marker.txt"
    sidecar.write_text(
        "[OCR skipped on page(s) 1-3]",
        encoding="utf-8",
        newline="",
    )

    sample = ocr_ops._sample_ocr_sidecar(
        sidecar,
        expected_pages=3,
        ineligible_pages=frozenset({1}),
    )

    assert sample.failed_page_markers == 2
    assert not sample.has_non_marker_content
    assert sample.tokens == ()


@pytest.mark.parametrize(
    "diagnostic",
    [
        "[tesseract] took too long to OCR - skipping",
        "page timed out during recognition",
        "page too big, skipping OCR",
        "image too large for Tesseract",
    ],
)
def test_exact_engine_skip_diagnostics_become_critical_warnings(diagnostic: str) -> None:
    warnings = ocr_ops._engine_diagnostic_warnings(diagnostic)
    assert len(warnings) == 1
    assert warnings[0].code == ocr_ops.OCR_ENGINE_PAGE_SKIPPED
    assert warnings[0].severity is WarningSeverity.CRITICAL


@pytest.mark.parametrize(
    "diagnostic",
    ["OCR completed page 1", "timeout configured to 300 seconds", "skipping optimization"],
)
def test_unrelated_engine_diagnostics_do_not_forge_page_skip_warning(diagnostic: str) -> None:
    assert ocr_ops._engine_diagnostic_warnings(diagnostic) == []


@pytest.mark.parametrize(
    ("engine_code", "ldf_code"),
    [(1, 1), (2, 1), (3, 3), (4, 4), (5, 1), (6, 1), (7, 1), (8, 1), (9, 1), (10, 4), (15, 1)],
)
def test_ocrmypdf_exit_codes_map_to_stable_ldf_codes(engine_code: int, ldf_code: int) -> None:
    failure = ocr_ops._tool_failure(ToolResult(returncode=engine_code, output="private"))
    assert failure.returncode == engine_code
    assert failure.ldf_exit_code == ldf_code
    assert "private" not in str(failure)


@pytest.mark.parametrize("engine_code", [3, 4, 6, 10, 15])
def test_nonzero_engine_exit_withholds_diagnostics_and_publishes_nothing(
    fixtures_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    engine_code: int,
) -> None:
    _patch_engine_requirements(monkeypatch)
    secret = "OCR-DIAGNOSTIC-SECRET-72E9"
    monkeypatch.setattr(
        ocr_ops,
        "run_tool",
        lambda *_args, **_kwargs: ToolResult(returncode=engine_code, output=secret),
    )
    output = tmp_path / "never.pdf"

    with pytest.raises(PipelineError) as failure:
        ocr_pdf(
            fixtures_dir / "ocr-image-only.pdf",
            output,
            options=OcrOptions(settings=_settings(tmp_path)),
        )

    assert isinstance(failure.value.__cause__, OcrToolFailure)
    assert failure.value.__cause__.returncode == engine_code
    assert secret not in str(failure.value)
    assert failure.value.report is not None
    assert secret not in failure.value.report.model_dump_json()
    assert not output.exists()
    _assert_workspace_clean(tmp_path)


def test_launch_failure_maps_to_missing_engine_without_leaking_tool_error(
    fixtures_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_engine_requirements(monkeypatch)
    monkeypatch.setattr(
        ocr_ops,
        "run_tool",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ToolError("PRIVATE-PATH-C:/users/name/document.pdf")
        ),
    )

    with pytest.raises(PipelineError, match="could not be launched safely") as failure:
        ocr_pdf(
            fixtures_dir / "ocr-image-only.pdf",
            tmp_path / "never.pdf",
            options=OcrOptions(settings=_settings(tmp_path)),
        )

    cause = failure.value.__cause__
    assert isinstance(cause, OcrToolFailure) and cause.ldf_exit_code == 3
    assert "PRIVATE-PATH" not in failure.value.report.model_dump_json()


def test_timeout_and_cancel_exit_are_reported_as_cancelled(
    fixtures_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_engine_requirements(monkeypatch)

    def timeout(*_args, **_kwargs):
        raise ToolTimeout("OCRmyPDF exceeded its 1s time limit and was terminated")

    monkeypatch.setattr(ocr_ops, "run_tool", timeout)
    with pytest.raises(PipelineError, match="time limit") as failure:
        ocr_pdf(
            fixtures_dir / "ocr-image-only.pdf",
            tmp_path / "never.pdf",
            options=OcrOptions(settings=_settings(tmp_path)),
        )
    assert failure.value.report is not None
    assert failure.value.report.status is ReportStatus.CANCELLED
    _assert_workspace_clean(tmp_path)

    monkeypatch.setattr(
        ocr_ops,
        "run_tool",
        lambda *_args, **_kwargs: ToolResult(returncode=130, output=""),
    )
    with pytest.raises(PipelineError, match="cancelled") as cancellation:
        ocr_pdf(
            fixtures_dir / "ocr-image-only.pdf",
            tmp_path / "also-never.pdf",
            options=OcrOptions(settings=_settings(tmp_path)),
        )
    assert cancellation.value.report.status is ReportStatus.CANCELLED


def test_valid_pdf_without_expected_ocr_tokens_fails_semantic_validation(
    fixtures_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_engine_requirements(monkeypatch)
    monkeypatch.setattr(
        ocr_ops,
        "run_tool",
        _successful_tool(
            fixtures_dir / "simple-3page.pdf",
            page_count=1,
            sidecar_text="UNMATCHED-SEMANTIC-TOKEN-8675309\n",
        ),
    )
    with pytest.raises(PipelineError, match="Validation failed") as failure:
        ocr_pdf(
            fixtures_dir / "ocr-image-only.pdf",
            tmp_path / "never.pdf",
            options=OcrOptions(settings=_settings(tmp_path)),
        )

    assert failure.value.report.validation is not None
    check = next(
        item
        for item in failure.value.report.validation.checks
        if item.name.endswith(":ocr-text-embedded")
    )
    assert not check.passed
    assert not (tmp_path / "never.pdf").exists()


def test_over_cap_nonempty_sidecar_cannot_publish_textless_candidate(
    fixtures_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_engine_requirements(monkeypatch)
    monkeypatch.setattr(
        ocr_ops,
        "run_tool",
        _successful_tool(
            fixtures_dir / "text-image-only.pdf",
            page_count=1,
            sidecar_text=("Alpha " * (ocr_ops._EXPECTED_TOKEN_READ_CHARS // len("Alpha ") + 2)),
        ),
    )
    output = tmp_path / "never-textless.pdf"

    with pytest.raises(PipelineError, match="Validation failed") as failure:
        ocr_pdf(
            fixtures_dir / "text-image-only.pdf",
            output,
            options=OcrOptions(settings=_settings(tmp_path)),
        )

    semantic = next(
        check
        for check in failure.value.report.validation.checks
        if check.name.endswith(":ocr-text-embedded")
    )
    assert not semantic.passed
    assert not output.exists()


@pytest.mark.parametrize(
    ("sidecar_text", "expected_detail"),
    [
        ("!!!\n", "nonblank sidecar"),
        ("A" * 129 + "\n", "nonblank sidecar"),
        ("[OCR skipped on pageant]", "matched 0 of"),
    ],
)
def test_unverifiable_nonblank_sidecar_cannot_use_blank_scan_exception(
    fixtures_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sidecar_text: str,
    expected_detail: str,
) -> None:
    _patch_engine_requirements(monkeypatch)
    monkeypatch.setattr(
        ocr_ops,
        "run_tool",
        _successful_tool(
            fixtures_dir / "text-image-only.pdf",
            page_count=1,
            sidecar_text=sidecar_text,
        ),
    )
    output = tmp_path / "never-unverifiable.pdf"

    with pytest.raises(PipelineError, match="Validation failed") as failure:
        ocr_pdf(
            fixtures_dir / "text-image-only.pdf",
            output,
            options=OcrOptions(settings=_settings(tmp_path)),
        )

    semantic = next(
        check
        for check in failure.value.report.validation.checks
        if check.name.endswith(":ocr-text-embedded")
    )
    assert not semantic.passed
    assert expected_detail in semantic.detail
    assert not output.exists()


def test_all_eligible_pages_reported_skipped_fail_without_publication(
    fixtures_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_engine_requirements(monkeypatch)
    monkeypatch.setattr(
        ocr_ops,
        "run_tool",
        _successful_tool(
            fixtures_dir / "simple-3page.pdf",
            page_count=1,
            sidecar_text="[skipped page]",
        ),
    )
    output = tmp_path / "never.pdf"
    sidecar = tmp_path / "never.txt"

    with pytest.raises(PipelineError):
        ocr_pdf(
            fixtures_dir / "ocr-image-only.pdf",
            output,
            options=OcrOptions(
                sidecar=sidecar,
                settings=_settings(tmp_path),
            ),
        )

    assert not output.exists()
    assert not sidecar.exists()
    _assert_workspace_clean(tmp_path)


def test_ocr_skip_marker_on_ldf_eligible_page_fails_without_publication(
    fixtures_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_engine_requirements(monkeypatch)
    monkeypatch.setattr(
        ocr_ops,
        "run_tool",
        _successful_tool(
            fixtures_dir / "text-image-only.pdf",
            page_count=1,
            sidecar_text="[OCR skipped on page(s) 1]",
        ),
    )
    output = tmp_path / "never-eligible-skip.pdf"

    with pytest.raises(PipelineError, match="skipped every OCR-eligible page"):
        ocr_pdf(
            fixtures_dir / "text-image-only.pdf",
            output,
            options=OcrOptions(settings=_settings(tmp_path)),
        )

    assert not output.exists()
    _assert_workspace_clean(tmp_path)


def test_partial_engine_page_skip_is_critical_but_other_pages_publish(
    fixtures_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pikepdf

    source = tmp_path / "two-page-scan.pdf"
    with pikepdf.open(fixtures_dir / "ocr-image-only.pdf") as original, pikepdf.new() as pdf:
        pdf.pages.append(original.pages[0])
        pdf.pages.append(original.pages[0])
        pdf.save(source)

    _patch_engine_requirements(monkeypatch)
    monkeypatch.setattr(
        ocr_ops,
        "run_tool",
        _successful_tool(
            fixtures_dir / "simple-3page.pdf",
            page_count=2,
            sidecar_text=("[skipped page]\fAlpha Section 2\nMARKER-ALPHA-PAGE-2\n"),
            diagnostic="[tesseract] took too long to OCR - skipping",
        ),
    )
    output = tmp_path / "partial.pdf"
    sidecar = tmp_path / "partial.txt"

    report = ocr_pdf(
        source,
        output,
        options=OcrOptions(sidecar=sidecar, settings=_settings(tmp_path)),
    )

    warning = _warning_map(report)[ocr_ops.OCR_ENGINE_PAGE_SKIPPED]
    assert warning.severity is WarningSeverity.CRITICAL
    assert report.validation is not None and report.validation.passed
    assert output.is_file() and sidecar.is_file()
    assert "[skipped page]" in sidecar.read_text(encoding="utf-8")


def test_late_internal_sidecar_skip_marker_remains_authoritative_without_public_sidecar(
    fixtures_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pikepdf

    source = tmp_path / "two-page-late-skip.pdf"
    with pikepdf.open(fixtures_dir / "ocr-image-only.pdf") as original, pikepdf.new() as pdf:
        pdf.pages.append(original.pages[0])
        pdf.pages.append(original.pages[0])
        pdf.save(source)

    late_marker_sidecar = (
        "Alpha\n" + "x" * (ocr_ops._EXPECTED_TOKEN_READ_CHARS - len("Alpha\n")) + "\f[skipped page]"
    )
    retained_diagnostics = "ordinary progress\n" + "x" * ocr_ops._MAX_TOOL_OUTPUT_BYTES
    _patch_engine_requirements(monkeypatch)
    monkeypatch.setattr(
        ocr_ops,
        "run_tool",
        _successful_tool(
            fixtures_dir / "simple-3page.pdf",
            page_count=2,
            sidecar_text=late_marker_sidecar,
            diagnostic=retained_diagnostics,
        ),
    )
    output = tmp_path / "late-skip.pdf"

    report = ocr_pdf(
        source,
        output,
        options=OcrOptions(settings=_settings(tmp_path)),
    )

    warning = _warning_map(report)[ocr_ops.OCR_ENGINE_PAGE_SKIPPED]
    assert warning.severity is WarningSeverity.CRITICAL
    assert "1 OCR-eligible page(s)" in warning.message
    assert "sidecar" not in warning.message.casefold()
    assert report.validation is not None and report.validation.passed
    assert output.is_file()


def test_semantic_validator_enforces_text_resource_limits(fixtures_dir: Path) -> None:
    ordinary = ocr_ops._ocr_pdf_validator(
        fixtures_dir / "simple-3page.pdf",
        expected_pages=3,
        expected_tokens=("alpha",),
        limits=ResourceLimits(),
    )
    bounded = ocr_ops._ocr_pdf_validator(
        fixtures_dir / "simple-3page.pdf",
        expected_pages=3,
        expected_tokens=("alpha",),
        limits=ResourceLimits(max_decompressed_bytes=1),
    )

    assert ordinary.passed
    assert not bounded.passed
    limit_check = next(
        check for check in bounded.checks if check.name == "ocr-text-resource-limits"
    )
    assert not limit_check.passed


def test_unsafe_page_geometry_fails_before_pdfium_render(
    fixtures_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pikepdf

    from localdocforge.validation import pdf_checks

    oversized = tmp_path / "unsafe-mediabox.pdf"
    with pikepdf.open(fixtures_dir / "text-image-only.pdf") as pdf:
        pdf.pages[0].obj["/MediaBox"] = pikepdf.Array([0, 0, 1_000_000, 1_000_000])
        pdf.save(oversized)

    render_calls: list[int] = []

    def forbidden_render(*_args, **_kwargs):
        render_calls.append(1)
        raise AssertionError("unsafe OCR page geometry must fail before page.render")

    monkeypatch.setattr(pdf_checks, "render_pdf_page", forbidden_render)
    callback_calls: list[int] = []
    result = ocr_ops._ocr_pdf_validator(
        oversized,
        expected_pages=1,
        expected_tokens=(),
        limits=ResourceLimits(max_image_pixels=1_000_000),
        check_cancelled=lambda: callback_calls.append(1),
    )

    assert not result.passed
    resource_check = next(
        check for check in result.checks if check.name == "ocr-render-resource-limits"
    )
    assert not resource_check.passed
    assert callback_calls
    assert render_calls == []


def test_operation_forwards_job_limits_to_semantic_validator(
    fixtures_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_engine_requirements(monkeypatch)
    monkeypatch.setattr(
        ocr_ops,
        "run_tool",
        _successful_tool(fixtures_dir / "simple-3page.pdf", page_count=1),
    )
    real_validator = ocr_ops._ocr_pdf_validator
    observed: list[ResourceLimits] = []
    callback_effects: list[tuple[int, int]] = []
    cancellation_checks = 0
    deadline_checks = 0
    real_check_cancelled = JobContext.check_cancelled
    real_remaining_timeout = ocr_ops._remaining_timeout

    def count_cancellation(context: JobContext) -> None:
        nonlocal cancellation_checks
        cancellation_checks += 1
        real_check_cancelled(context)

    def count_deadline(context: JobContext) -> float:
        nonlocal deadline_checks
        deadline_checks += 1
        return real_remaining_timeout(context)

    monkeypatch.setattr(JobContext, "check_cancelled", count_cancellation)
    monkeypatch.setattr(ocr_ops, "_remaining_timeout", count_deadline)

    def capture_limits(path: Path, **kwargs):
        observed.append(kwargs["limits"])
        callback = kwargs["check_cancelled"]
        before = (cancellation_checks, deadline_checks)
        callback()
        callback_effects.append((cancellation_checks - before[0], deadline_checks - before[1]))
        return real_validator(path, **kwargs)

    monkeypatch.setattr(ocr_ops, "_ocr_pdf_validator", capture_limits)
    settings = _settings(tmp_path, max_decompressed_bytes=123_456)

    report = ocr_pdf(
        fixtures_dir / "ocr-image-only.pdf",
        tmp_path / "bounded.pdf",
        options=OcrOptions(settings=settings),
    )

    assert report.validation is not None and report.validation.passed
    assert observed == [settings.limits]
    assert callback_effects == [(1, 1)]


def test_malformed_candidate_and_missing_sidecar_fail_before_publication(
    fixtures_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_engine_requirements(monkeypatch)

    def malformed(_tool: str, args: list[str], **_kwargs) -> ToolResult:
        Path(args[-1]).write_bytes(b"not a PDF")
        Path(args[args.index("--sidecar") + 1]).write_text("text\n", encoding="utf-8")
        return ToolResult(returncode=0, output="")

    monkeypatch.setattr(ocr_ops, "run_tool", malformed)
    with pytest.raises(PipelineError, match="Validation failed"):
        ocr_pdf(
            fixtures_dir / "ocr-image-only.pdf",
            tmp_path / "malformed.pdf",
            options=OcrOptions(settings=_settings(tmp_path)),
        )

    def no_sidecar(_tool: str, args: list[str], **_kwargs) -> ToolResult:
        _write_reference_pdf(fixtures_dir / "simple-3page.pdf", Path(args[-1]), 1)
        return ToolResult(returncode=0, output="")

    monkeypatch.setattr(ocr_ops, "run_tool", no_sidecar)
    with pytest.raises(PipelineError, match="without producing its required sidecar"):
        ocr_pdf(
            fixtures_dir / "ocr-image-only.pdf",
            tmp_path / "missing-sidecar.pdf",
            options=OcrOptions(settings=_settings(tmp_path)),
        )
    assert not (tmp_path / "malformed.pdf").exists()
    assert not (tmp_path / "missing-sidecar.pdf").exists()
    _assert_workspace_clean(tmp_path)


def test_non_utf8_sidecar_is_refused_before_publication(
    fixtures_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_engine_requirements(monkeypatch)

    def invalid_sidecar(_tool: str, args: list[str], **_kwargs) -> ToolResult:
        _write_reference_pdf(fixtures_dir / "simple-3page.pdf", Path(args[-1]), 1)
        Path(args[args.index("--sidecar") + 1]).write_bytes(b"\xff\xfe")
        return ToolResult(returncode=0, output="")

    monkeypatch.setattr(ocr_ops, "run_tool", invalid_sidecar)
    with pytest.raises(PipelineError, match="strict UTF-8"):
        ocr_pdf(
            fixtures_dir / "ocr-image-only.pdf",
            tmp_path / "never.pdf",
            options=OcrOptions(
                sidecar=tmp_path / "never.txt",
                settings=_settings(tmp_path),
            ),
        )
    assert not (tmp_path / "never.pdf").exists()
    assert not (tmp_path / "never.txt").exists()
    _assert_workspace_clean(tmp_path)


def test_signed_input_reports_critical_signature_invalidation(
    fixtures_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pikepdf

    source = tmp_path / "signed-scan.pdf"
    staged = tmp_path / "signed-scan.staged.pdf"
    shutil.copyfile(fixtures_dir / "ocr-image-only.pdf", source)
    with pikepdf.open(source) as pdf:
        page = pdf.pages[0]
        signature = pdf.make_indirect(
            pikepdf.Dictionary(
                Type=pikepdf.Name("/Sig"),
                Filter=pikepdf.Name("/Adobe.PPKLite"),
                SubFilter=pikepdf.Name("/adbe.pkcs7.detached"),
                ByteRange=pikepdf.Array([0, 0, 0, 0]),
                Contents=pikepdf.String("SYNTHETIC-NOT-A-REAL-SIGNATURE"),
            )
        )
        widget = pdf.make_indirect(
            pikepdf.Dictionary(
                Type=pikepdf.Name("/Annot"),
                Subtype=pikepdf.Name("/Widget"),
                FT=pikepdf.Name("/Sig"),
                T=pikepdf.String("SyntheticSignature"),
                Rect=pikepdf.Array([20, 20, 120, 50]),
                P=page.obj,
                V=signature,
                F=4,
            )
        )
        page.obj["/Annots"] = pikepdf.Array([widget])
        pdf.Root["/AcroForm"] = pikepdf.Dictionary(Fields=pikepdf.Array([widget]))
        pdf.save(staged)
    staged.replace(source)
    before = _digest(source)
    _patch_engine_requirements(monkeypatch)
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        ocr_ops,
        "run_tool",
        _successful_tool(
            fixtures_dir / "simple-3page.pdf",
            page_count=1,
            captured=captured,
        ),
    )

    report = ocr_pdf(
        source,
        tmp_path / "ocr-signed.pdf",
        options=OcrOptions(settings=_settings(tmp_path)),
    )

    warnings = {item.code: item for item in report.security_warnings}
    assert warnings["signature-invalidated"].severity is WarningSeverity.CRITICAL
    args = captured["args"]
    assert isinstance(args, list)
    assert "--invalidate-digital-signatures" in args
    assert _digest(source) == before


def test_encrypted_input_is_decrypted_privately_and_reports_critical_warning(
    fixtures_dir: Path,
    fixture_password: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = fixtures_dir / "encrypted.pdf"
    before = _digest(source)
    _patch_engine_requirements(monkeypatch)
    monkeypatch.setattr(
        ocr_ops,
        "run_tool",
        _successful_tool(fixtures_dir / "simple-3page.pdf", page_count=3),
    )

    report = ocr_pdf(
        source,
        tmp_path / "decrypted-ocr.pdf",
        options=OcrOptions(
            force_ocr=True,
            password=fixture_password,
            settings=_settings(tmp_path),
        ),
    )

    warnings = {item.code: item for item in report.security_warnings}
    assert warnings["input-encryption-removed"].severity is WarningSeverity.CRITICAL
    assert _digest(source) == before


def test_sidecar_is_byte_deterministic_and_blank_scan_is_accepted(
    fixtures_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_engine_requirements(monkeypatch)
    monkeypatch.setattr(
        ocr_ops,
        "run_tool",
        _successful_tool(
            fixtures_dir / "simple-3page.pdf",
            page_count=1,
            sidecar_text="Alpha Section 1\r\nMARKER-ALPHA-PAGE-1\r\n",
        ),
    )
    sidecars = [tmp_path / "first.txt", tmp_path / "second.txt"]
    for index, sidecar in enumerate(sidecars):
        ocr_pdf(
            fixtures_dir / "ocr-image-only.pdf",
            tmp_path / f"result-{index}.pdf",
            options=OcrOptions(sidecar=sidecar, settings=_settings(tmp_path)),
        )
    assert sidecars[0].read_bytes() == sidecars[1].read_bytes()
    assert b"\r" not in sidecars[0].read_bytes()

    def blank(_tool: str, args: list[str], **_kwargs) -> ToolResult:
        shutil.copyfile(fixtures_dir / "text-image-only.pdf", Path(args[-1]))
        Path(args[args.index("--sidecar") + 1]).write_text("", encoding="utf-8")
        return ToolResult(returncode=0, output="")

    monkeypatch.setattr(ocr_ops, "run_tool", blank)
    report = ocr_pdf(
        fixtures_dir / "text-image-only.pdf",
        tmp_path / "blank.pdf",
        options=OcrOptions(settings=_settings(tmp_path)),
    )
    semantic = next(
        check for check in report.validation.checks if check.name.endswith(":ocr-text-embedded")
    )
    assert semantic.passed and "blank scan accepted" in semantic.detail


def test_sidecar_normalization_duplication_is_charged_to_temporary_limit(
    fixtures_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_engine_requirements(monkeypatch)
    sidecar_text = "Alpha Section 1\n" * 500
    monkeypatch.setattr(
        ocr_ops,
        "run_tool",
        _successful_tool(
            fixtures_dir / "simple-3page.pdf",
            page_count=1,
            sidecar_text=sidecar_text,
        ),
    )
    source = fixtures_dir / "ocr-image-only.pdf"
    limit = source.stat().st_size + 15_000

    with pytest.raises(PipelineError, match="temporary files total"):
        ocr_pdf(
            source,
            tmp_path / "never.pdf",
            options=OcrOptions(
                sidecar=tmp_path / "never.txt",
                settings=_settings(tmp_path, max_temporary_bytes=limit),
            ),
        )

    assert not (tmp_path / "never.pdf").exists()
    assert not (tmp_path / "never.txt").exists()
    _assert_workspace_clean(tmp_path)


@pytest.mark.parametrize(
    ("limits", "message"),
    [
        ({"max_pages": 0}, "over the configured limit"),
        ({"max_subprocesses": 1}, "max_subprocesses limit of at least 2"),
        ({"max_temporary_bytes": 1}, "temporary files total"),
    ],
)
def test_ocr_resource_preflights_reject_before_engine_launch(
    fixtures_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limits: dict[str, int],
    message: str,
) -> None:
    _patch_engine_requirements(monkeypatch)

    def unexpected(*_args, **_kwargs):
        raise AssertionError("OCRmyPDF must not launch after a failed resource preflight")

    monkeypatch.setattr(ocr_ops, "run_tool", unexpected)
    with pytest.raises(PipelineError, match=message):
        ocr_pdf(
            fixtures_dir / "ocr-image-only.pdf",
            tmp_path / "never.pdf",
            options=OcrOptions(settings=_settings(tmp_path, **limits)),
        )
    assert not (tmp_path / "never.pdf").exists()
    _assert_workspace_clean(tmp_path)


def test_zero_image_pixel_limit_rejects_before_engine_launch(
    fixtures_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_engine_requirements(monkeypatch)
    launched_argv: list[list[str]] = []

    def unexpected(_tool: str, args: list[str], **_kwargs):
        launched_argv.append(list(args))
        raise AssertionError("zero max_image_pixels must reject before OCRmyPDF launch")

    monkeypatch.setattr(ocr_ops, "run_tool", unexpected)
    with pytest.raises(PipelineError) as failure:
        ocr_pdf(
            fixtures_dir / "ocr-image-only.pdf",
            tmp_path / "never.pdf",
            options=OcrOptions(
                settings=_settings(tmp_path, max_image_pixels=0),
            ),
        )

    assert "max_image_pixels" in str(failure.value)
    assert launched_argv == []
    assert all("--max-image-mpixels" not in args for args in launched_argv)
    assert not (tmp_path / "never.pdf").exists()
    _assert_workspace_clean(tmp_path)


def test_output_limit_prevents_pdf_and_sidecar_partial_publication(
    fixtures_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_engine_requirements(monkeypatch)
    monkeypatch.setattr(
        ocr_ops,
        "run_tool",
        _successful_tool(fixtures_dir / "simple-3page.pdf", page_count=1),
    )
    output = tmp_path / "never.pdf"
    sidecar = tmp_path / "never.txt"
    with pytest.raises(PipelineError, match="output limit"):
        ocr_pdf(
            fixtures_dir / "ocr-image-only.pdf",
            output,
            options=OcrOptions(
                sidecar=sidecar,
                settings=_settings(tmp_path, max_output_bytes=100),
            ),
        )
    assert not output.exists() and not sidecar.exists()
    _assert_workspace_clean(tmp_path)


@pytest.fixture(scope="module")
def real_ocr_engines() -> dict[str, EngineInfo]:
    registry = EngineRegistry()
    infos: dict[str, EngineInfo] = {}
    missing: list[str] = []
    for name in ("ocrmypdf", "tesseract", "ghostscript"):
        engine = registry.get(name)
        if engine is None:
            missing.append(name)
            continue
        info = engine.probe()
        infos[name] = info
        if not info.available:
            missing.append(name)
    if missing:
        pytest.skip("real OCR integration requires all engines: " + ", ".join(missing))
    tesseract_path = infos["tesseract"].path
    assert tesseract_path is not None
    try:
        languages = ocr_ops._installed_tesseract_languages(tesseract_path)
    except EngineUnavailableError as exc:
        pytest.skip(f"real OCR language discovery unavailable: {exc}")
    if "eng" not in languages:
        pytest.skip("real OCR integration requires the Tesseract eng language pack")
    return infos


def test_real_ocr_validates_pdfium_text_and_deterministic_sidecar(
    fixtures_dir: Path,
    tmp_path: Path,
    real_ocr_engines: dict[str, EngineInfo],
) -> None:
    sidecars = [tmp_path / "real-first.txt", tmp_path / "real-second.txt"]
    reports = []
    for index, sidecar in enumerate(sidecars):
        reports.append(
            ocr_pdf(
                fixtures_dir / "ocr-image-only.pdf",
                tmp_path / f"real-{index}.pdf",
                options=OcrOptions(sidecar=sidecar, settings=_settings(tmp_path)),
            )
        )

    extracted = _extract_text(tmp_path / "real-0.pdf").casefold()
    assert "alpha section 1" in extracted
    assert "synthetic fixture text" in extracted
    assert sidecars[0].read_bytes() == sidecars[1].read_bytes()
    assert all(report.validation and report.validation.passed for report in reports)
    assert all(ocr_ops.OCR_TEXT_APPROXIMATE in _warning_map(report) for report in reports)
    assert reports[0].engine_version == real_ocr_engines["ocrmypdf"].version
