"""Integration and adversarial tests for Markdown-to-PDF via real Typst."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from urllib.parse import quote

import pikepdf
import pytest
from PIL import Image, PngImagePlugin

import localdocforge.operations.markdown as markdown_ops
from localdocforge.config.settings import Settings
from localdocforge.domain.models import ReportStatus, ResourceLimits
from localdocforge.engines.registry import EngineRegistry
from localdocforge.jobs.workspace import CollisionPolicy
from localdocforge.operations.markdown import (
    MARKDOWN_CONSTRUCT_DROPPED,
    SYSTEM_FONT_DEPENDENT,
    MdToPdfOptions,
    md_to_pdf,
)
from localdocforge.operations.text import PdfToMdOptions, pdf_to_md
from localdocforge.pipelines.runner import PipelineError
from localdocforge.security.subproc import ToolResult, ToolTimeout
from localdocforge.validation.pdf_checks import render_pdf_page


@pytest.fixture(scope="module")
def typst_info():
    engine = EngineRegistry().get("typst")
    if engine is None:
        pytest.skip("Typst adapter is not registered")
    info = engine.probe()
    if not info.available:
        pytest.skip(f"Typst >=0.15.1 is unavailable: {info.notes or info.install_hint}")
    return info


def _settings(root: Path, *, limits: ResourceLimits | None = None) -> Settings:
    return Settings(jobs_root=root / "jobs", limits=limits or ResourceLimits())


def _options(
    root: Path,
    *,
    paper: str = "A4",
    margin_mm: float = 20.0,
    toc: bool = False,
    collision: CollisionPolicy | None = None,
    limits: ResourceLimits | None = None,
) -> MdToPdfOptions:
    return MdToPdfOptions(
        paper=paper,
        margin_mm=margin_mm,
        toc=toc,
        collision=collision,
        settings=_settings(root, limits=limits),
    )


def _write_markdown(root: Path, source: str, *, name: str = "report.md") -> Path:
    path = root / name
    path.write_text(source, encoding="utf-8", newline="\n")
    return path


def _extract_text(pdf_path: Path, root: Path) -> str:
    output = root / f"{pdf_path.stem}-extracted.txt"
    pdf_to_md(
        pdf_path,
        output,
        options=PdfToMdOptions(
            output_format="txt",
            page_anchors=False,
            settings=Settings(jobs_root=root / f"extract-{pdf_path.stem}"),
        ),
    )
    return output.read_text(encoding="utf-8")


def _page_size(path: Path) -> tuple[float, float]:
    with pikepdf.open(path) as pdf:
        page = pdf.pages[0]
        return (
            float(page.mediabox[2]) - float(page.mediabox[0]),
            float(page.mediabox[3]) - float(page.mediabox[1]),
        )


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _assert_workspace_clean(root: Path) -> None:
    jobs = root / "jobs"
    assert not jobs.exists() or not list(jobs.glob("ldf-job-*"))


def _make_windows_junction(link: Path, target: Path) -> None:
    command = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")
    result = subprocess.run(  # noqa: S603 - fixed Windows shell and mklink builtin
        [command, "/d", "/c", "mklink", "/J", str(link), str(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"directory junction creation unavailable: {result.stderr.strip()}")


def test_commonmark_gfm_table_and_code_round_trip(
    tmp_path: Path,
    typst_info,
) -> None:
    source = _write_markdown(
        tmp_path,
        """# AGENT-REPORT-TITLE

Paragraph with *EMPHASIS-MARKER*, **STRONG-MARKER**, and `INLINE-CODE-MARKER`.

- BULLET-ALPHA
- BULLET-BETA

1. ORDERED-ONE
2. ORDERED-TWO

> BLOCKQUOTE-MARKER

[LINK-LABEL-MARKER](https://example.invalid/report)

| Column A | Column B |
| --- | --- |
| TABLE-CELL-ALPHA | TABLE-CELL-BETA |

```python
FENCED-CODE-MARKER
```
""",
    )
    output = tmp_path / "report.pdf"

    report = md_to_pdf(source, output, options=_options(tmp_path))

    assert report.status is ReportStatus.SUCCESS
    assert report.engine == "typst"
    assert report.engine_version == typst_info.version
    assert report.validation is not None and report.validation.passed
    assert report.output_page_count == report.outputs[0].page_count
    assert (
        report.details
        | {
            "paper": "A4",
            "margin_mm": 20.0,
            "toc": False,
            "image_count": 0,
        }
        == report.details
    )
    assert SYSTEM_FONT_DEPENDENT in {warning.code for warning in report.fidelity_warnings}
    text = _extract_text(output, tmp_path)
    for marker in (
        "AGENT-REPORT-TITLE",
        "EMPHASIS-MARKER",
        "STRONG-MARKER",
        "INLINE-CODE-MARKER",
        "BULLET-ALPHA",
        "BULLET-BETA",
        "ORDERED-ONE",
        "ORDERED-TWO",
        "BLOCKQUOTE-MARKER",
        "LINK-LABEL-MARKER",
        "TABLE-CELL-ALPHA",
        "TABLE-CELL-BETA",
        "FENCED-CODE-MARKER",
    ):
        assert marker in text
    _assert_workspace_clean(tmp_path)


def test_currency_dollar_text_round_trips_without_math_warning(
    tmp_path: Path,
    typst_info,
) -> None:
    source = _write_markdown(
        tmp_path,
        """# CURRENCY-TEXT

The price range is $5-$10 for members.

Price is $5 and$10 total.

The single-item fee is $20.
""",
    )
    output = tmp_path / "currency.pdf"

    report = md_to_pdf(source, output, options=_options(tmp_path))

    assert not any(
        warning.code == MARKDOWN_CONSTRUCT_DROPPED
        for warning in report.fidelity_warnings
    )
    assert report.details["dropped_constructs"] == []
    text = " ".join(_extract_text(output, tmp_path).split())
    assert "The price range is $5-$10 for members." in text
    assert "Price is $5 and$10 total." in text
    assert "The single-item fee is $20." in text
    _assert_workspace_clean(tmp_path)


@pytest.mark.parametrize(
    ("paper", "expected"),
    [
        ("A4", (595.28, 841.89)),
        ("Letter", (612.0, 792.0)),
        ("Legal", (612.0, 1008.0)),
    ],
)
def test_named_paper_sizes_and_margin(
    tmp_path: Path,
    typst_info,
    paper: str,
    expected: tuple[float, float],
) -> None:
    source = _write_markdown(tmp_path, "# PAGE-SIZE-MARKER\n\nBody.\n")
    output = tmp_path / f"{paper.casefold()}.pdf"

    report = md_to_pdf(
        source,
        output,
        options=_options(tmp_path, paper=paper.swapcase(), margin_mm=12.5),
    )

    width, height = _page_size(output)
    assert width == pytest.approx(expected[0], abs=0.8)
    assert height == pytest.approx(expected[1], abs=0.8)
    assert report.details["paper"] == paper
    assert report.details["margin_mm"] == 12.5


@pytest.mark.parametrize("margin", [-1.0, float("inf"), float("nan"), 105.0])
def test_invalid_margin_is_refused_before_engine_execution(tmp_path: Path, margin: float) -> None:
    source = _write_markdown(tmp_path, "Body\n")
    with pytest.raises(PipelineError, match="Margin"):
        md_to_pdf(source, tmp_path / "never.pdf", options=_options(tmp_path, margin_mm=margin))
    assert not (tmp_path / "never.pdf").exists()


def test_toc_adds_contents_page_and_preserves_headings(tmp_path: Path, typst_info) -> None:
    source = _write_markdown(
        tmp_path,
        "# TOC-SECTION-ALPHA\n\nAlpha body.\n\n## TOC-SECTION-BETA\n\nBeta body.\n",
    )
    output = tmp_path / "toc.pdf"

    report = md_to_pdf(source, output, options=_options(tmp_path, toc=True))

    assert report.details["toc"] is True
    assert report.output_page_count is not None and report.output_page_count >= 2
    text = _extract_text(output, tmp_path)
    assert "Contents" in text
    assert "TOC-SECTION-ALPHA" in text
    assert "TOC-SECTION-BETA" in text


def test_local_image_is_validated_normalized_and_embedded(tmp_path: Path, typst_info) -> None:
    image_path = tmp_path / "diagram.png"
    with Image.new("RGB", (96, 48), (220, 20, 30)) as image:
        image.save(image_path)
    source = _write_markdown(
        tmp_path,
        "# IMAGE-DOCUMENT-MARKER\n\n![IMAGE-ALT-MARKER](diagram.png)\n",
    )
    output = tmp_path / "image.pdf"

    report = md_to_pdf(source, output, options=_options(tmp_path))

    assert report.details["image_count"] == 1
    assert report.details["images_normalized_to_png"] == 1
    assert len(report.inputs) == 2
    assert report.inputs[1].media_type == "image/png"
    page = render_pdf_page(output, 0, scale=1.0).convert("RGB")
    try:
        reduced = page.resize((32, 32))
        try:
            assert any(
                red > 150 and green < 100 and blue < 100
                for red, green, blue in reduced.get_flattened_data()
            )
        finally:
            reduced.close()
    finally:
        page.close()
    assert "IMAGE-DOCUMENT-MARKER" in _extract_text(output, tmp_path)


def test_image_pixel_and_decompressed_limits_fail_before_publication(
    tmp_path: Path,
    typst_info,
) -> None:
    image_path = tmp_path / "bounded.png"
    with Image.new("RGB", (15, 10), "purple") as image:
        image.save(image_path)
    source = _write_markdown(tmp_path, "![bounded](bounded.png)\n")

    pixel_output = tmp_path / "pixel-limit.pdf"
    with pytest.raises(PipelineError, match="150 pixels.*100-pixel limit"):
        md_to_pdf(
            source,
            pixel_output,
            options=_options(
                tmp_path,
                limits=ResourceLimits(max_image_pixels=100),
            ),
        )
    assert not pixel_output.exists()
    _assert_workspace_clean(tmp_path)

    decoded_output = tmp_path / "decoded-limit.pdf"
    with pytest.raises(PipelineError, match="Decoded Markdown images exceed"):
        md_to_pdf(
            source,
            decoded_output,
            options=_options(
                tmp_path,
                limits=ResourceLimits(max_decompressed_bytes=100),
            ),
        )
    assert not decoded_output.exists()
    _assert_workspace_clean(tmp_path)


def test_image_normalization_strips_profiles_exif_and_text_chunks(tmp_path: Path) -> None:
    image_path = tmp_path / "metadata.png"
    png_info = PngImagePlugin.PngInfo()
    png_info.add_text("Comment", "ATTACKER-CONTROLLED-COMMENT")
    with Image.new("RGB", (8, 6), "purple") as image:
        exif = Image.Exif()
        exif[270] = "ATTACKER-CONTROLLED-EXIF"
        image.save(
            image_path,
            format="PNG",
            pnginfo=png_info,
            icc_profile=b"ATTACKER-CONTROLLED-ICC",
            exif=exif,
        )
    normalized = tmp_path / "normalized.png"

    with image_path.open("rb") as source_stream:
        markdown_ops._normalize_asset(
            markdown_ops._Asset(image_path, "assets/image-0001.png", 7),
            normalized,
            source_stream=source_stream,
            media_type="image/png",
            max_pixels=1_000,
            remaining_decompressed_bytes=1_000,
            decompressed_limit=1_000,
        )

    with Image.open(normalized) as image:
        image.load()
        assert "icc_profile" not in image.info
        assert "exif" not in image.info
        assert "Comment" not in image.info


def test_image_format_swap_after_pipeline_sniff_is_resniffed_before_decode(
    tmp_path: Path,
    typst_info,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_path = tmp_path / "race.png"
    with Image.new("RGB", (16, 16), "red") as image:
        image.save(image_path)
    original_size = image_path.stat().st_size
    replacement = b"%!PS-Adobe-3.0\n%%Title: refused\nshowpage\n"
    assert len(replacement) < original_size
    replacement += b" " * (original_size - len(replacement))
    source = _write_markdown(tmp_path, "![race](race.png)\n")
    output = tmp_path / "never.pdf"
    real_verify = markdown_ops._verify_markdown_snapshot

    def verify_then_swap(*args, **kwargs) -> None:
        real_verify(*args, **kwargs)
        image_path.write_bytes(replacement)

    monkeypatch.setattr(markdown_ops, "_verify_markdown_snapshot", verify_then_swap)
    monkeypatch.setattr(
        markdown_ops,
        "_normalize_asset",
        lambda *_args, **_kwargs: pytest.fail("decoded a post-sniff format swap"),
    )

    with pytest.raises(PipelineError, match="changed to unsupported content"):
        md_to_pdf(source, output, options=_options(tmp_path))

    assert not output.exists()
    _assert_workspace_clean(tmp_path)


def test_source_and_parser_preprocessing_bounds_fail_before_tool_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oversized = tmp_path / "oversized.md"
    oversized.write_bytes(b"123456789")
    settings = _settings(
        tmp_path,
        limits=ResourceLimits(
            max_input_bytes=8,
            max_memory_bytes=None,
            max_temporary_bytes=None,
        ),
    )
    monkeypatch.setattr(
        markdown_ops,
        "require_media_type",
        lambda *_args, **_kwargs: pytest.fail("sniffed an already-oversize source"),
    )
    with pytest.raises(PipelineError, match="safe preprocessing limit of 8 bytes"):
        markdown_ops._load_markdown_source(oversized, settings)

    monkeypatch.undo()
    many_lines = tmp_path / "many-lines.md"
    many_lines.write_text("one\u2028two\u2029three", encoding="utf-8")
    monkeypatch.setattr(markdown_ops, "_MAX_MARKDOWN_SOURCE_LINES", 2)
    with pytest.raises(PipelineError, match="3 lines.*limit of 2"):
        markdown_ops._load_markdown_source(many_lines, _settings(tmp_path))

    tokens = markdown_ops._markdown_parser().parse("- one\n- two\n")
    monkeypatch.setattr(markdown_ops, "_MAX_MARKDOWN_TOKENS", 1)
    with pytest.raises(PipelineError, match="token stream exceeds"):
        markdown_ops._enforce_token_limit(tokens)


def test_markdown_image_reference_count_is_bounded(tmp_path: Path) -> None:
    references = [
        markdown_ops._ImageReference(target=f"image-{index}.png", alt="", line=index + 1)
        for index in range(257)
    ]

    with pytest.raises(PipelineError, match="257 image references.*limit of 256"):
        markdown_ops._enforce_image_reference_limit(references)


def test_local_image_applies_exif_orientation_before_embedding(
    tmp_path: Path,
    typst_info,
) -> None:
    image_path = tmp_path / "rotated.jpg"
    exif = Image.Exif()
    exif[274] = 6  # 90° clockwise: stored 40x20 becomes displayed 20x40.
    with Image.new("RGB", (40, 20), "orange") as image:
        image.save(image_path, format="JPEG", exif=exif)
    source = _write_markdown(tmp_path, "![rotated](rotated.jpg)\n")
    output = tmp_path / "rotated.pdf"

    md_to_pdf(source, output, options=_options(tmp_path))

    with pikepdf.open(output) as pdf:
        xobjects = pdf.pages[0].Resources.get("/XObject", {})
        image_sizes = [
            (int(item.Width), int(item.Height))
            for item in xobjects.values()
            if item.get("/Subtype") == "/Image"
        ]
    assert any(height > width for width, height in image_sizes)


def test_unicode_metacharacter_image_name_is_neutralized(
    tmp_path: Path,
    typst_info,
) -> None:
    name = "résumé #@$.png"
    with Image.new("RGB", (24, 12), "green") as image:
        image.save(tmp_path / name)
    source = _write_markdown(tmp_path, f"![safe](<{quote(name)}>)\n")
    output = tmp_path / "neutral.pdf"

    report = md_to_pdf(source, output, options=_options(tmp_path))

    assert report.status is ReportStatus.SUCCESS
    assert report.details["image_count"] == 1
    assert name not in str(report.details)


def test_unsupported_constructs_are_dropped_with_stable_lines(
    tmp_path: Path,
    typst_info,
) -> None:
    source = _write_markdown(
        tmp_path,
        """# SUPPORTED-HEADING

<div>RAW-HTML-SECRET</div>

[^note]: FOOTNOTE-DEFINITION-SECRET

Visible sentence [^note] and $DOLLAR-TEXT-MARKER$.

$$
BLOCK-MATH-SECRET
$$

SUPPORTED-TAIL with ~~STRIKETHROUGH-TEXT~~.
""",
    )
    output = tmp_path / "dropped.pdf"

    report = md_to_pdf(source, output, options=_options(tmp_path))

    dropped = {(item["construct"], item["line"]) for item in report.details["dropped_constructs"]}
    assert {
        ("raw HTML", 3),
        ("footnote definition", 5),
        ("footnote reference", 7),
        ("math block", 9),
        ("strikethrough", 13),
    } <= dropped
    warnings = [
        warning
        for warning in report.fidelity_warnings
        if warning.code == MARKDOWN_CONSTRUCT_DROPPED
    ]
    assert len(warnings) == len(dropped)
    assert all(
        "line " in warning.message and "was dropped" in warning.message for warning in warnings
    )
    text = _extract_text(output, tmp_path)
    assert "SUPPORTED-HEADING" in text and "SUPPORTED-TAIL" in text
    assert "STRIKETHROUGH-TEXT" in text
    assert "$DOLLAR-TEXT-MARKER$" in text
    for secret in (
        "RAW-HTML-SECRET",
        "FOOTNOTE-DEFINITION-SECRET",
        "BLOCK-MATH-SECRET",
    ):
        assert secret not in text
        assert secret not in report.model_dump_json()


def test_dropped_construct_reporting_is_bounded(tmp_path: Path, typst_info) -> None:
    definitions = "\n\n".join(
        f"[^note-{index}]: DROPPED-SECRET-{index}" for index in range(300)
    )
    source = _write_markdown(tmp_path, f"# BOUNDED-WARNINGS\n\n{definitions}\n")
    output = tmp_path / "bounded-warnings.pdf"

    report = md_to_pdf(source, output, options=_options(tmp_path))

    assert report.details["dropped_constructs_truncated"] is True
    assert report.details["dropped_constructs_omitted"] == 44
    assert report.details["dropped_construct_report_limit"] == 256
    assert len(report.details["dropped_constructs"]) == 257
    warning_codes = [
        warning.code
        for warning in report.fidelity_warnings
        if warning.code == MARKDOWN_CONSTRUCT_DROPPED
    ]
    assert len(warning_codes) == 257
    serialized = report.model_dump_json()
    assert "DROPPED-SECRET" not in serialized
    assert "additional unsupported constructs" in serialized


def test_typst_injection_corpus_renders_literally_and_cannot_read_outside_root(
    tmp_path: Path,
    typst_info,
) -> None:
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("OUTSIDE-FILE-CONTENT-MUST-NOT-APPEAR", encoding="utf-8")
    source = _write_markdown(
        tmp_path,
        r"""# INJECTION-CORPUS

Literal calls: #eval("1 + 1") #read("../outside-secret.txt") #import("evil.typ") @preview/evil:1.0.0

Escapes: \\ [brackets] $dollar$ `backtick` @at *star* _underscore_ <angle@example.invalid>

`#eval("inline-code")`

```typst
#read("../outside-secret.txt")
#import "@preview/evil:1.0.0": *
```
""",
    )
    output = tmp_path / "injection.pdf"

    report = md_to_pdf(source, output, options=_options(tmp_path))

    assert report.status is ReportStatus.SUCCESS
    text = _extract_text(output, tmp_path)
    for literal in (
        "#eval",
        "#read",
        "#import",
        "@preview",
        "$dollar$",
        "outside-secret.txt",
    ):
        assert literal in text
    assert "OUTSIDE-FILE-CONTENT-MUST-NOT-APPEAR" not in text
    assert "OUTSIDE-FILE-CONTENT-MUST-NOT-APPEAR" not in report.model_dump_json()
    assert report.details["typst_root"] == "private-job-workspace"
    assert report.details["typst_packages"] == "disabled-and-audited-empty"


@pytest.mark.parametrize(
    "target",
    [
        "../outside.png",
        "%2e%2e/outside.png",
        "https://example.invalid/image.png",
        "data:image/png;base64,AAAA",
        "//server/share/image.png",
        "missing.png?query=1",
    ],
)
def test_image_escape_and_remote_paths_are_refused(tmp_path: Path, target: str) -> None:
    with Image.new("RGB", (4, 4), "red") as image:
        image.save(tmp_path / "outside.png")
    document_dir = tmp_path / "document"
    document_dir.mkdir()
    source = _write_markdown(document_dir, f"![x]({target})\n")
    output = tmp_path / "never.pdf"

    with pytest.raises(PipelineError, match="Markdown image"):
        md_to_pdf(source, output, options=_options(tmp_path))

    assert not output.exists()


def test_symlinked_image_cannot_escape_document_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside.png"
    with Image.new("RGB", (4, 4), "red") as image:
        image.save(outside)
    document_dir = tmp_path / "document"
    document_dir.mkdir()
    linked = document_dir / "linked.png"
    try:
        linked.symlink_to(outside)
    except (NotImplementedError, OSError):
        pytest.skip("symlink creation is unavailable")
    source = _write_markdown(document_dir, "![x](linked.png)\n")

    with pytest.raises(PipelineError, match="Markdown image"):
        md_to_pdf(source, tmp_path / "never.pdf", options=_options(tmp_path))


@pytest.mark.skipif(os.name != "nt", reason="Windows junction/reparse regression")
def test_junctioned_image_cannot_escape_document_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    with Image.new("RGB", (4, 4), "red") as image:
        image.save(outside / "secret.png")
    document_dir = tmp_path / "document"
    document_dir.mkdir()
    junction = document_dir / "redirect"
    _make_windows_junction(junction, outside)
    source = _write_markdown(document_dir, "![x](redirect/secret.png)\n")

    try:
        with pytest.raises(PipelineError, match="Markdown image"):
            md_to_pdf(source, tmp_path / "never.pdf", options=_options(tmp_path))
    finally:
        if junction.exists():
            junction.rmdir()


def test_referenced_asset_cannot_be_overwritten_as_output(tmp_path: Path, typst_info) -> None:
    protected = tmp_path / "protected.pdf"
    with Image.new("RGB", (16, 16), "blue") as image:
        image.save(protected, format="PNG")
    before = protected.read_bytes()
    source = _write_markdown(tmp_path, "![protected](protected.pdf)\n")

    with pytest.raises(PipelineError, match="aliases an input"):
        md_to_pdf(
            source,
            protected,
            options=_options(tmp_path, collision=CollisionPolicy.OVERWRITE),
        )

    assert protected.read_bytes() == before
    _assert_workspace_clean(tmp_path)


def test_output_byte_and_page_limits_fail_closed(tmp_path: Path, typst_info) -> None:
    short_source = _write_markdown(tmp_path, "# OUTPUT-LIMIT-MARKER\n")
    byte_output = tmp_path / "byte-limit.pdf"
    with pytest.raises(PipelineError, match="Generated outputs total"):
        md_to_pdf(
            short_source,
            byte_output,
            options=_options(
                tmp_path,
                limits=ResourceLimits(max_output_bytes=128),
            ),
        )
    assert not byte_output.exists()
    _assert_workspace_clean(tmp_path)

    long_source = _write_markdown(
        tmp_path,
        "# PAGE-LIMIT-MARKER\n\n"
        + "\n\n".join(f"Paragraph {index}: " + ("bounded content " * 80) for index in range(30)),
        name="long.md",
    )
    page_output = tmp_path / "page-limit.pdf"
    with pytest.raises(PipelineError, match="Generated PDF has .* over the configured limit of 1"):
        md_to_pdf(
            long_source,
            page_output,
            options=_options(tmp_path, limits=ResourceLimits(max_pages=1)),
        )
    assert not page_output.exists()
    _assert_workspace_clean(tmp_path)


def test_collision_fail_rename_and_workspace_cleanup(tmp_path: Path, typst_info) -> None:
    source = _write_markdown(tmp_path, "# COLLISION-MARKER\n")
    output = tmp_path / "collision.pdf"
    md_to_pdf(
        source,
        output,
        options=_options(tmp_path, collision=CollisionPolicy.FAIL),
    )
    before = _digest(output)

    with pytest.raises(PipelineError, match="Output already exists"):
        md_to_pdf(
            source,
            output,
            options=_options(tmp_path, collision=CollisionPolicy.FAIL),
        )
    assert _digest(output) == before

    renamed = md_to_pdf(
        source,
        output,
        options=_options(tmp_path, collision=CollisionPolicy.RENAME),
    )
    assert renamed.outputs[0].path.name == "collision (1).pdf"
    assert renamed.outputs[0].path.is_file()
    _assert_workspace_clean(tmp_path)


def test_nonzero_typst_exit_withholds_diagnostics_and_cleans_workspace(
    tmp_path: Path,
    typst_info,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document_secret = "DOCUMENT-TEXT-SECRET-4F91"
    diagnostic_secret = "TOOL-DIAGNOSTIC-SECRET-7A22"
    source = _write_markdown(tmp_path, f'# {document_secret}\n\n#read("outside")\n')
    output = tmp_path / "never.pdf"
    captured: dict[str, object] = {}

    def fail_tool(tool: str, args: list[str], **kwargs) -> ToolResult:
        captured.update(tool=tool, args=list(args), kwargs=dict(kwargs))
        return ToolResult(returncode=17, output=diagnostic_secret)

    monkeypatch.setattr(markdown_ops, "run_tool", fail_tool)
    with pytest.raises(PipelineError, match="diagnostics were withheld") as failure:
        md_to_pdf(source, output, options=_options(tmp_path))

    assert captured["tool"] == "typst"
    args = captured["args"]
    assert isinstance(args, list)
    assert args[0] == "compile"
    assert "--root" in args and "--package-path" in args and "--package-cache-path" in args
    assert args[args.index("--jobs") + 1] == "1"
    assert document_secret not in " ".join(args)
    assert diagnostic_secret not in str(failure.value)
    assert failure.value.report is not None
    serialized = failure.value.report.model_dump_json()
    assert document_secret not in serialized
    assert diagnostic_secret not in serialized
    assert not output.exists()
    _assert_workspace_clean(tmp_path)


def test_markdown_snapshot_change_is_refused_before_compile(
    tmp_path: Path,
    typst_info,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_markdown(tmp_path, "# ORIGINAL\n")
    output = tmp_path / "never.pdf"
    real_run_pipeline = markdown_ops.run_pipeline

    def mutate_then_run(**kwargs):
        source.write_text("# MUTATION\n", encoding="utf-8", newline="\n")
        return real_run_pipeline(**kwargs)

    monkeypatch.setattr(markdown_ops, "run_pipeline", mutate_then_run)
    with pytest.raises(PipelineError, match="changed after preprocessing"):
        md_to_pdf(source, output, options=_options(tmp_path))

    assert not output.exists()
    _assert_workspace_clean(tmp_path)


def test_malformed_typst_candidate_uses_standard_failed_validation(
    tmp_path: Path,
    typst_info,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_markdown(tmp_path, "# MALFORMED-CANDIDATE\n")
    output = tmp_path / "never.pdf"

    def malformed_tool(_tool: str, args: list[str], **_kwargs) -> ToolResult:
        source_path = Path(args[-2])
        candidate = Path(args[-1])
        dependency_manifest = Path(args[args.index("--deps") + 1])
        candidate.write_bytes(b"not a PDF")
        dependency_manifest.write_text(
            json.dumps(
                {
                    "inputs": [str(source_path)],
                    "outputs": [str(candidate)],
                }
            ),
            encoding="utf-8",
        )
        return ToolResult(returncode=0, output="")

    monkeypatch.setattr(markdown_ops, "run_tool", malformed_tool)
    with pytest.raises(PipelineError, match="Validation failed") as failure:
        md_to_pdf(source, output, options=_options(tmp_path))

    assert failure.value.report is not None
    assert failure.value.report.validation is not None
    assert failure.value.report.validation.passed is False
    assert not output.exists()
    _assert_workspace_clean(tmp_path)


def test_non_object_typst_dependency_manifest_is_controlled(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    dependency_manifest = workspace / "deps.json"
    dependency_manifest.write_text("[]", encoding="utf-8")

    with pytest.raises(PipelineError, match="invalid dependency manifest"):
        markdown_ops._audit_typst_dependencies(
            dependency_manifest,
            workspace=workspace,
            expected_inputs=[],
            expected_output=workspace / "candidate.pdf",
        )


def test_typst_timeout_is_cancelled_without_output_or_text_leak(
    tmp_path: Path,
    typst_info,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "TIMEOUT-DOCUMENT-SECRET-1B39"
    source = _write_markdown(tmp_path, f"# {marker}\n")
    output = tmp_path / "never.pdf"

    def timeout_tool(_tool: str, _args: list[str], **_kwargs) -> ToolResult:
        raise ToolTimeout("typst exceeded its 1s time limit and was terminated")

    monkeypatch.setattr(markdown_ops, "run_tool", timeout_tool)
    with pytest.raises(PipelineError, match="time limit") as failure:
        md_to_pdf(source, output, options=_options(tmp_path))

    assert failure.value.report is not None
    assert failure.value.report.status is ReportStatus.CANCELLED
    assert marker not in failure.value.report.model_dump_json()
    assert not output.exists()
    _assert_workspace_clean(tmp_path)
