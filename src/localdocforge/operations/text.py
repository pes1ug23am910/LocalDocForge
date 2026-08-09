"""Deterministic PDF text extraction to Markdown, plain text, or JSONL.

PDFium supplies Unicode text and geometry.  The small layout pass in this
module is deliberately conservative: headings, columns, and ruled tables are
heuristics, and every such inference is surfaced through stable warning codes
and per-page coverage metadata.
"""

from __future__ import annotations

import json
import math
import re
import statistics
import unicodedata
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from localdocforge.config.settings import Settings
from localdocforge.domain.models import (
    ConversionReport,
    FidelityWarning,
    InputArtifact,
    JobContext,
    ProgressCallback,
    ResourceLimits,
    SecurityWarning,
    ValidationCheck,
    ValidationResult,
    WarningSeverity,
)
from localdocforge.domain.pages import PageRange
from localdocforge.engines.adapters import OP_PDF_TO_MD
from localdocforge.engines.registry import default_registry
from localdocforge.jobs.workspace import CollisionPolicy
from localdocforge.operations.organize import _open_pdf
from localdocforge.pipelines.runner import (
    CandidateOutput,
    ExecuteResult,
    PipelineError,
    run_pipeline,
)

TEXT_OUTPUT_FORMATS = ("md", "txt", "jsonl")

_MEDIA_TYPES = {
    "md": "text/markdown",
    "txt": "text/plain",
    "jsonl": "application/x-ndjson",
}
_MD_ANCHOR = re.compile(r"(?m)^<!-- ldf:page ([1-9]\d*) -->$")
_TXT_ANCHOR = re.compile(r"(?m)^--- ldf:page ([1-9]\d*) ---$")
_SOURCE_MD_ANCHOR = re.compile(
    r"(?m)^([ \t]*)<!--[ \t]*ldf:page[ \t]+\d+[ \t]*-->[ \t]*$"
)
_SOURCE_TXT_ANCHOR = re.compile(
    r"(?m)^([ \t]*)---[ \t]*ldf:page[ \t]+\d+[ \t]*---[ \t]*$"
)
_INLINE_SPACE = re.compile(r"[^\S\n]+")

NO_TEXT_LAYER = "no-text-layer"
HEADINGS_INFERRED = "headings-inferred"
READING_ORDER_UNCERTAIN = "reading-order-uncertain"
TABLES_FLATTENED = "tables-flattened"
TABLE_FIDELITY_BEST_EFFORT = "table-fidelity-best-effort"
WARNING_CODE_ORDER = (
    NO_TEXT_LAYER,
    HEADINGS_INFERRED,
    READING_ORDER_UNCERTAIN,
    TABLE_FIDELITY_BEST_EFFORT,
    TABLES_FLATTENED,
)
_MAX_LAYOUT_RECTS_PER_PAGE = 50_000
_MAX_PAGE_OBJECTS_PER_PAGE = 4_096
_MIN_OUTPUT_PREFLIGHT_CHARS = 1_000_000
_MAX_TABLE_EDGES_PER_PAGE = 1_024
_MAX_TABLE_INTERSECTION_PAIRS = 4_096
_MAX_TABLE_PATH_SEGMENTS_PER_PAGE = 8_192
_MAX_TABLES_PER_PAGE = 32
_MAX_TABLE_CELLS_PER_PAGE = 4_096
_MAX_TABLE_TEXT_BYTES_PER_PAGE = 4 * 1024 * 1024
_TABLE_COORDINATE_TOLERANCE = 3.0
_TABLE_SETTINGS = {
    "vertical_strategy": "lines",
    "horizontal_strategy": "lines",
    "snap_tolerance": 3,
    "join_tolerance": 3,
    "edge_min_length_prefilter": 3,
    "edge_min_length": 3,
    "intersection_tolerance": 3,
}


@dataclass
class PdfToMdOptions:
    """Options shared by the library, CLI, and localhost API surfaces."""

    output_format: str = "md"
    pages: PageRange | None = None
    page_anchors: bool = True
    tables: bool = False
    collision: CollisionPolicy | None = None
    settings: Settings | None = None
    progress: ProgressCallback | None = None
    password: str | None = None


@dataclass(frozen=True)
class _Fragment:
    text: str
    left: float
    bottom: float
    right: float
    top: float
    font_size: float
    angle: float
    source_index: int

    @property
    def height(self) -> float:
        return max(0.0, self.top - self.bottom)


@dataclass(frozen=True)
class _Line:
    text: str
    left: float
    bottom: float
    right: float
    top: float
    font_size: float

    @property
    def height(self) -> float:
        return max(0.0, self.top - self.bottom)


@dataclass(frozen=True)
class _PageText:
    plain_text: str
    markdown_text: str
    has_text_layer: bool
    warning_codes: tuple[str, ...]
    emitted_tables: int = 0
    flattened_table_candidates: int = 0

    @property
    def char_count(self) -> int:
        return len(self.plain_text)


@dataclass(frozen=True)
class _ObjectScan:
    has_text_object: bool
    ruled_table: bool
    truncated: bool
    table_analysis_safe: bool = True


@dataclass(frozen=True)
class _MarkdownTable:
    bbox: tuple[float, float, float, float]
    markdown: str
    plain_text: str
    row_count: int
    column_count: int


@dataclass(frozen=True)
class _TableExtraction:
    tables: tuple[_MarkdownTable, ...] = ()
    flattened_candidates: int = 0
    plumber_bbox: tuple[float, float, float, float] | None = None


def _normalize_text(value: str) -> str:
    """Normalize transport details without silently rewriting content."""

    value = unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))
    lines = [_INLINE_SPACE.sub(" ", line).rstrip() for line in value.split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def _normalize_fragment(value: str) -> str:
    return _INLINE_SPACE.sub(" ", _normalize_text(value).replace("\n", " ")).strip()


def _nonspace_signature(value: str) -> Counter[str]:
    return Counter(
        character
        for character in unicodedata.normalize("NFC", value)
        if not character.isspace()
    )


def _clip_rect(
    rect: tuple[float, float, float, float],
    page_box: tuple[float, float, float, float],
) -> tuple[float, float, float, float] | None:
    left = max(rect[0], page_box[0])
    bottom = max(rect[1], page_box[1])
    right = min(rect[2], page_box[2])
    top = min(rect[3], page_box[3])
    if not all(math.isfinite(value) for value in (left, bottom, right, top)):
        return None
    if right <= left or top <= bottom:
        return None
    return left, bottom, right, top


def _fragment_style(textpage: Any, rect: tuple[float, float, float, float]) -> tuple[float, float]:
    """Sample a character in a rectangle for font size and angle."""

    import pypdfium2.raw as pdfium_c

    left, bottom, right, top = rect
    width = max(1.0, right - left)
    height = max(1.0, top - bottom)
    points = (
        (left + min(1.0, width / 4), (bottom + top) / 2),
        ((left + right) / 2, (bottom + top) / 2),
    )
    for x, y in points:
        index = textpage.get_index(x, y, width / 2, height / 2)
        if index is None:
            continue
        try:
            size = float(pdfium_c.FPDFText_GetFontSize(textpage.raw, index))
            angle = float(pdfium_c.FPDFText_GetCharAngle(textpage.raw, index))
        except (TypeError, ValueError, OSError):
            continue
        if math.isfinite(size) and size > 0 and math.isfinite(angle):
            return size, angle
    return 0.0, 0.0


def _extract_fragments(
    page: Any,
    textpage: Any,
    *,
    byte_limit: int | None,
    sample_style: bool,
    check_cancelled: Callable[[], None] | None,
) -> tuple[list[_Fragment], bool]:
    """Extract bounded line fragments."""

    page_box = tuple(float(value) for value in page.get_bbox())
    fragments: list[_Fragment] = []
    rect_count = textpage.count_rects()
    rect_limit = _MAX_LAYOUT_RECTS_PER_PAGE
    if byte_limit is not None:
        rect_limit = min(rect_limit, max(1, byte_limit + 1))
    if rect_count > rect_limit:
        return [], True
    fragment_bytes = 0
    for index in range(rect_count):
        if check_cancelled is not None and index % 128 == 0:
            check_cancelled()
        clipped = _clip_rect(tuple(float(value) for value in textpage.get_rect(index)), page_box)
        if clipped is None:
            continue
        text = _normalize_fragment(textpage.get_text_bounded(*clipped, errors="strict"))
        if not text:
            continue
        fragment_bytes += len(text.encode("utf-8", errors="strict"))
        if byte_limit is not None and fragment_bytes > byte_limit:
            raise PipelineError(
                "Bounded PDFium fragments exceed the configured extraction byte limit"
            )
        font_size, angle = _fragment_style(textpage, clipped) if sample_style else (0.0, 0.0)
        fragments.append(
            _Fragment(
                text=text,
                left=clipped[0],
                bottom=clipped[1],
                right=clipped[2],
                top=clipped[3],
                font_size=font_size,
                angle=angle,
                source_index=index,
            )
        )
    return fragments, False


def _same_line(first: _Fragment, second: _Fragment) -> bool:
    first_center = (first.bottom + first.top) / 2
    second_center = (second.bottom + second.top) / 2
    tolerance = max(2.0, min(first.height, second.height) * 0.35)
    return abs(first_center - second_center) <= tolerance


def _lines_from_fragments(
    fragments: list[_Fragment],
    page_width: float,
    check_cancelled: Callable[[], None] | None,
) -> tuple[list[_Line], bool, bool]:
    """Sort in user space and join same-baseline fragments left-to-right."""

    ordered = sorted(fragments, key=lambda item: (-item.top, item.left, item.source_index))
    groups: list[list[_Fragment]] = []
    for index, fragment in enumerate(ordered):
        if check_cancelled is not None and index % 128 == 0:
            check_cancelled()
        if groups and _same_line(groups[-1][0], fragment):
            groups[-1].append(fragment)
        else:
            groups.append([fragment])

    lines: list[_Line] = []
    wide_gap_lines = 0
    repeated_column_lines = 0
    aligned_row_patterns: dict[tuple[int, ...], int] = {}
    processed_fragments = 0
    for group in groups:
        group.sort(key=lambda item: (item.left, item.source_index))
        if len(group) >= 3:
            starts = tuple(fragment.left for fragment in group[:16])
            if all(
                second - first >= 24.0
                for first, second in zip(starts, starts[1:], strict=False)
            ):
                pattern = tuple(round(start / 6.0) for start in starts)
                aligned_row_patterns[pattern] = aligned_row_patterns.get(pattern, 0) + 1
        pieces: list[str] = []
        wide_gap = False
        separated_starts = False
        previous: _Fragment | None = None
        for fragment in group:
            processed_fragments += 1
            if check_cancelled is not None and processed_fragments % 128 == 0:
                check_cancelled()
            joiner = ""
            if previous is not None:
                gap = fragment.left - previous.right
                wide_gap = wide_gap or gap > max(36.0, page_width * 0.12)
                separated_starts = separated_starts or (
                    fragment.left - previous.left > max(72.0, page_width * 0.20)
                )
                touching_tolerance = max(1.0, min(previous.height, fragment.height) * 0.10)
                if gap > touching_tolerance:
                    joiner = " "
            if joiner:
                pieces.append(joiner)
            pieces.append(fragment.text)
            previous = fragment
        if wide_gap:
            wide_gap_lines += 1
        if separated_starts:
            repeated_column_lines += 1
        sizes = [item.font_size for item in group if item.font_size > 0]
        lines.append(
            _Line(
                text="".join(pieces),
                left=min(item.left for item in group),
                bottom=min(item.bottom for item in group),
                right=max(item.right for item in group),
                top=max(item.top for item in group),
                font_size=statistics.median(sizes) if sizes else 0.0,
            )
        )
    multiple_columns = wide_gap_lines >= 2 or repeated_column_lines >= 2
    possible_unruled_table = any(count >= 3 for count in aligned_row_patterns.values())
    return lines, multiple_columns, possible_unruled_table


def _paragraph_blocks(lines: list[_Line]) -> list[list[_Line]]:
    if not lines:
        return []
    heights = [line.height for line in lines if line.height > 0]
    typical_height = statistics.median(heights) if heights else 10.0
    blocks: list[list[_Line]] = [[lines[0]]]
    for line in lines[1:]:
        previous = blocks[-1][-1]
        vertical_gap = previous.bottom - line.top
        indent_shift = abs(line.left - previous.left)
        if vertical_gap > max(typical_height * 1.15, 7.0) or indent_shift > 24.0:
            blocks.append([line])
        else:
            blocks[-1].append(line)
    return blocks


def _heading_levels(lines: list[_Line]) -> dict[int, int]:
    """Return line-index to Markdown heading level for conservative candidates."""

    sizes = [line.font_size for line in lines if line.font_size > 0 and line.text]
    if len(sizes) < 2:
        return {}
    body_size = statistics.median(sizes)
    candidates = {
        index: line.font_size
        for index, line in enumerate(lines)
        if line.font_size >= max(body_size * 1.2, body_size + 1.5)
        and len(line.text) <= 200
        and not line.text.endswith((".", ";", ","))
    }
    if not candidates:
        return {}
    ranked_sizes = sorted({round(size, 2) for size in candidates.values()}, reverse=True)
    level_by_size = {
        size: min(6, index + 1) for index, size in enumerate(ranked_sizes)
    }
    return {
        index: level_by_size[round(size, 2)]
        for index, size in candidates.items()
    }


def _render_plain(lines: list[_Line]) -> str:
    return "\n\n".join("\n".join(line.text for line in block) for block in _paragraph_blocks(lines))


def _render_markdown(lines: list[_Line], heading_levels: dict[int, int]) -> str:
    if not lines:
        return ""
    blocks: list[str] = []
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            blocks.append("\n".join(paragraph))
            paragraph.clear()

    heights = [line.height for line in lines if line.height > 0]
    typical_height = statistics.median(heights) if heights else 10.0
    previous: _Line | None = None
    for index, line in enumerate(lines):
        level = heading_levels.get(index)
        if level is not None:
            flush()
            blocks.append(f"{'#' * level} {line.text}")
        else:
            if previous is not None and index - 1 not in heading_levels:
                vertical_gap = previous.bottom - line.top
                indent_shift = abs(line.left - previous.left)
                if vertical_gap > max(typical_height * 1.15, 7.0) or indent_shift > 24.0:
                    flush()
            paragraph.append(line.text)
        previous = line
    flush()
    return "\n\n".join(blocks)


def _escape_gfm_cell(value: str) -> str:
    """Keep extracted cell text inside one deterministic GFM table cell."""

    normalized = _normalize_text(value)
    escaped = normalized.replace("\\", "\\\\").replace("|", "\\|")
    return "<br>".join(escaped.split("\n"))


def _bbox_is_finite(bbox: tuple[float, float, float, float]) -> bool:
    return all(math.isfinite(value) for value in bbox) and bbox[2] > bbox[0] and bbox[3] > bbox[1]


def _page_geometry_compatible(
    page_box: tuple[float, float, float, float],
    plumber_bbox: tuple[float, float, float, float],
) -> bool:
    if not _bbox_is_finite(page_box) or not _bbox_is_finite(plumber_bbox):
        return False
    page_width = page_box[2] - page_box[0]
    page_height = page_box[3] - page_box[1]
    plumber_width = plumber_bbox[2] - plumber_bbox[0]
    plumber_height = plumber_bbox[3] - plumber_bbox[1]
    return math.isclose(page_width, plumber_width, rel_tol=0.001, abs_tol=1.0) and math.isclose(
        page_height, plumber_height, rel_tol=0.001, abs_tol=1.0
    )


def _pdf_point_to_plumber(
    x: float,
    y: float,
    *,
    page_box: tuple[float, float, float, float],
    plumber_bbox: tuple[float, float, float, float],
) -> tuple[float, float]:
    page_width = page_box[2] - page_box[0]
    page_height = page_box[3] - page_box[1]
    plumber_width = plumber_bbox[2] - plumber_bbox[0]
    plumber_height = plumber_bbox[3] - plumber_bbox[1]
    mapped_x = plumber_bbox[0] + ((x - page_box[0]) / page_width) * plumber_width
    mapped_top = plumber_bbox[1] + ((page_box[3] - y) / page_height) * plumber_height
    return mapped_x, mapped_top


def _fragment_in_table(
    fragment: _Fragment,
    table: _MarkdownTable,
    *,
    page_box: tuple[float, float, float, float],
    plumber_bbox: tuple[float, float, float, float],
) -> bool:
    left, top = _pdf_point_to_plumber(
        fragment.left,
        fragment.top,
        page_box=page_box,
        plumber_bbox=plumber_bbox,
    )
    right, bottom = _pdf_point_to_plumber(
        fragment.right,
        fragment.bottom,
        page_box=page_box,
        plumber_bbox=plumber_bbox,
    )
    table_left, table_top, table_right, table_bottom = table.bbox
    tolerance = _TABLE_COORDINATE_TOLERANCE
    return (
        left >= table_left - tolerance
        and right <= table_right + tolerance
        and top >= table_top - tolerance
        and bottom <= table_bottom + tolerance
    )


def _fragment_overlaps_table(
    fragment: _Fragment,
    table: _MarkdownTable,
    *,
    page_box: tuple[float, float, float, float],
    plumber_bbox: tuple[float, float, float, float],
) -> bool:
    left, top = _pdf_point_to_plumber(
        fragment.left,
        fragment.top,
        page_box=page_box,
        plumber_bbox=plumber_bbox,
    )
    right, bottom = _pdf_point_to_plumber(
        fragment.right,
        fragment.bottom,
        page_box=page_box,
        plumber_bbox=plumber_bbox,
    )
    table_left, table_top, table_right, table_bottom = table.bbox
    tolerance = _TABLE_COORDINATE_TOLERANCE
    return (
        min(right, table_right) - max(left, table_left) > tolerance
        and min(bottom, table_bottom) - max(top, table_top) > tolerance
    )


def _apply_table_regions(
    fragments: list[_Fragment],
    extraction: _TableExtraction,
    *,
    page_box: tuple[float, float, float, float],
) -> tuple[list[_Fragment], _TableExtraction]:
    plumber_bbox = extraction.plumber_bbox
    if not extraction.tables or plumber_bbox is None:
        return fragments, extraction

    rejected = extraction.flattened_candidates
    accepted: list[_MarkdownTable] = []
    contained_by_table: list[set[int]] = []
    for table in extraction.tables:
        contained: set[int] = set()
        partial_overlap = False
        for index, fragment in enumerate(fragments):
            if _fragment_in_table(
                fragment,
                table,
                page_box=page_box,
                plumber_bbox=plumber_bbox,
            ):
                contained.add(index)
            elif _fragment_overlaps_table(
                fragment,
                table,
                page_box=page_box,
                plumber_bbox=plumber_bbox,
            ):
                partial_overlap = True
                break
        if partial_overlap or not contained:
            rejected += 1
            continue
        pdfium_text = "".join(fragments[index].text for index in sorted(contained))
        if _nonspace_signature(pdfium_text) != _nonspace_signature(table.plain_text):
            rejected += 1
            continue
        accepted.append(table)
        contained_by_table.append(contained)

    removed = set().union(*contained_by_table) if contained_by_table else set()
    outside = [fragment for index, fragment in enumerate(fragments) if index not in removed]
    return outside, _TableExtraction(
        tables=tuple(accepted),
        flattened_candidates=rejected,
        plumber_bbox=plumber_bbox,
    )


def _table_bboxes_overlap(first: _MarkdownTable, second: _MarkdownTable) -> bool:
    horizontal = min(first.bbox[2], second.bbox[2]) - max(first.bbox[0], second.bbox[0])
    vertical = min(first.bbox[3], second.bbox[3]) - max(first.bbox[1], second.bbox[1])
    # Shared borders are fine, but any positive-area overlap would let the
    # same source character satisfy two candidate signatures and be emitted
    # twice. Bboxes are already in one pdfplumber coordinate system here, so
    # no cross-engine tolerance is appropriate.
    return horizontal > 0.0 and vertical > 0.0


def _render_markdown_with_tables(
    lines: list[_Line],
    heading_levels: dict[int, int],
    tables: tuple[_MarkdownTable, ...],
    *,
    page_box: tuple[float, float, float, float],
    plumber_bbox: tuple[float, float, float, float],
) -> str:
    events: list[tuple[float, int, float, int, _Line | _MarkdownTable]] = []
    for index, line in enumerate(lines):
        x, top = _pdf_point_to_plumber(
            line.left,
            (line.bottom + line.top) / 2,
            page_box=page_box,
            plumber_bbox=plumber_bbox,
        )
        events.append((top, 0, x, index, line))
    for index, table in enumerate(tables):
        events.append((table.bbox[1], 1, table.bbox[0], index, table))
    events.sort(key=lambda item: item[:4])

    blocks: list[str] = []
    line_group: list[tuple[int, _Line]] = []

    def flush_lines() -> None:
        if not line_group:
            return
        group_lines = [line for _, line in line_group]
        local_levels = {
            local_index: heading_levels[source_index]
            for local_index, (source_index, _) in enumerate(line_group)
            if source_index in heading_levels
        }
        rendered = _render_markdown(group_lines, local_levels)
        if rendered:
            blocks.append(rendered)
        line_group.clear()

    for _, kind, _, source_index, item in events:
        if kind == 0:
            assert isinstance(item, _Line)
            line_group.append((source_index, item))
        else:
            assert isinstance(item, _MarkdownTable)
            flush_lines()
            blocks.append(item.markdown)
    flush_lines()
    return "\n\n".join(blocks)


def _render_plain_with_tables(
    lines: list[_Line],
    tables: tuple[_MarkdownTable, ...],
    *,
    page_box: tuple[float, float, float, float],
    plumber_bbox: tuple[float, float, float, float],
) -> str:
    events: list[tuple[float, int, float, _Line | _MarkdownTable]] = []
    for line in lines:
        x, top = _pdf_point_to_plumber(
            line.left,
            (line.bottom + line.top) / 2,
            page_box=page_box,
            plumber_bbox=plumber_bbox,
        )
        events.append((top, 0, x, line))
    for table in tables:
        events.append((table.bbox[1], 1, table.bbox[0], table))
    events.sort(key=lambda item: item[:3])

    blocks: list[str] = []
    line_group: list[_Line] = []

    def flush_lines() -> None:
        if line_group:
            rendered = _render_plain(line_group)
            if rendered:
                blocks.append(rendered)
            line_group.clear()

    for _, kind, _, item in events:
        if kind == 0:
            assert isinstance(item, _Line)
            line_group.append(item)
        else:
            assert isinstance(item, _MarkdownTable)
            flush_lines()
            blocks.append(item.plain_text)
    flush_lines()
    return "\n\n".join(blocks)


def _extract_markdown_tables(
    table_page: Any | None,
    *,
    page_box: tuple[float, float, float, float],
    rotated: bool,
    object_scan: _ObjectScan,
    possible_unruled_table: bool,
    byte_limit: int | None,
    memory_limit: int | None,
    check_cancelled: Callable[[], None] | None,
) -> _TableExtraction:
    """Return only rectangular, bounded line tables; every other hint is flattened."""

    fallback_evidence = object_scan.ruled_table or possible_unruled_table
    if (
        table_page is None
        or rotated
        or object_scan.truncated
        or not object_scan.table_analysis_safe
    ):
        return _TableExtraction(flattened_candidates=int(fallback_evidence))
    if check_cancelled is not None:
        check_cancelled()
    try:
        plumber_bbox = tuple(float(value) for value in table_page.bbox)
    except (AttributeError, TypeError, ValueError, OverflowError, OSError):
        return _TableExtraction(flattened_candidates=int(fallback_evidence))
    if len(plumber_bbox) != 4 or not _page_geometry_compatible(page_box, plumber_bbox):
        return _TableExtraction(flattened_candidates=int(fallback_evidence))

    try:
        edges = list(table_page.edges)
    except Exception:  # pdfminer failures are an honest flowed-text fallback
        return _TableExtraction(
            flattened_candidates=int(fallback_evidence), plumber_bbox=plumber_bbox
        )
    try:
        vertical_edges = sum(edge.get("orientation") == "v" for edge in edges)
        horizontal_edges = sum(edge.get("orientation") == "h" for edge in edges)
    except (AttributeError, TypeError):
        return _TableExtraction(
            flattened_candidates=int(fallback_evidence), plumber_bbox=plumber_bbox
        )
    if (
        len(edges) > _MAX_TABLE_EDGES_PER_PAGE
        or vertical_edges * horizontal_edges > _MAX_TABLE_INTERSECTION_PAIRS
    ):
        return _TableExtraction(
            flattened_candidates=int(fallback_evidence), plumber_bbox=plumber_bbox
        )
    if vertical_edges < 3 or horizontal_edges < 3:
        return _TableExtraction(
            flattened_candidates=int(fallback_evidence), plumber_bbox=plumber_bbox
        )

    if check_cancelled is not None:
        check_cancelled()
    try:
        raw_tables = list(table_page.find_tables(_TABLE_SETTINGS))
    except Exception:  # do not expose parser diagnostics or fail a safe fallback
        return _TableExtraction(
            flattened_candidates=int(fallback_evidence), plumber_bbox=plumber_bbox
        )
    if len(raw_tables) > _MAX_TABLES_PER_PAGE:
        return _TableExtraction(
            flattened_candidates=len(raw_tables), plumber_bbox=plumber_bbox
        )
    try:
        plumber_chars = list(table_page.chars)
    except Exception:
        return _TableExtraction(
            flattened_candidates=max(1, int(fallback_evidence)),
            plumber_bbox=plumber_bbox,
        )
    if len(plumber_chars) > _MAX_LAYOUT_RECTS_PER_PAGE:
        return _TableExtraction(
            flattened_candidates=max(1, int(fallback_evidence)),
            plumber_bbox=plumber_bbox,
        )

    table_text_limit = _MAX_TABLE_TEXT_BYTES_PER_PAGE
    if byte_limit is not None:
        table_text_limit = min(table_text_limit, max(0, byte_limit))
    if memory_limit is not None:
        table_text_limit = min(table_text_limit, max(0, memory_limit // 64))
    table_text_bytes = 0
    cell_count = 0
    rejected = 0
    provisional: list[_MarkdownTable] = []
    for raw_table in raw_tables:
        if check_cancelled is not None:
            check_cancelled()
        try:
            bbox = tuple(float(value) for value in raw_table.bbox)
            rows = list(raw_table.rows)
            row_cells = [list(row.cells) for row in rows]
        except Exception:
            rejected += 1
            continue
        if len(bbox) != 4 or not _bbox_is_finite(bbox):
            rejected += 1
            continue
        if (
            bbox[0] < plumber_bbox[0] - _TABLE_COORDINATE_TOLERANCE
            or bbox[1] < plumber_bbox[1] - _TABLE_COORDINATE_TOLERANCE
            or bbox[2] > plumber_bbox[2] + _TABLE_COORDINATE_TOLERANCE
            or bbox[3] > plumber_bbox[3] + _TABLE_COORDINATE_TOLERANCE
        ):
            rejected += 1
            continue
        if len(row_cells) < 2 or not row_cells or len(row_cells[0]) < 2:
            rejected += 1
            continue
        column_count = len(row_cells[0])
        current_cells = len(row_cells) * column_count
        cell_count += current_cells
        if (
            cell_count > _MAX_TABLE_CELLS_PER_PAGE
            or any(len(row) != column_count for row in row_cells)
            or any(cell is None for row in row_cells for cell in row)
        ):
            rejected += 1
            continue
        try:
            matrix = raw_table.extract()
        except Exception:
            rejected += 1
            continue
        if (
            not isinstance(matrix, list)
            or len(matrix) != len(row_cells)
            or any(not isinstance(row, list) or len(row) != column_count for row in matrix)
            or any(cell is None or not isinstance(cell, str) for row in matrix for cell in row)
        ):
            rejected += 1
            continue
        normalized = [[_normalize_text(cell) for cell in row] for row in matrix]
        if any(not cell for cell in normalized[0]) or not all(
            any(cell for cell in row) for row in normalized[1:]
        ):
            rejected += 1
            continue
        try:
            region_text = "".join(
                str(character.get("text", ""))
                for character in plumber_chars
                if bbox[0]
                <= (float(character.get("x0", 0.0)) + float(character.get("x1", 0.0)))
                / 2
                <= bbox[2]
                and bbox[1]
                <= (
                    float(character.get("top", 0.0))
                    + float(character.get("bottom", 0.0))
                )
                / 2
                <= bbox[3]
            )
        except (AttributeError, TypeError, ValueError, OverflowError):
            rejected += 1
            continue
        cell_text = "".join(cell for row in normalized for cell in row)
        region_signature = _nonspace_signature(region_text)
        cell_signature = _nonspace_signature(cell_text)
        if not region_signature or region_signature != cell_signature:
            rejected += 1
            continue
        table_text_bytes += sum(
            len(cell.encode("utf-8", errors="strict")) for row in normalized for cell in row
        )
        if table_text_bytes > table_text_limit:
            rejected += 1
            continue
        rendered_rows = [
            "| " + " | ".join(_escape_gfm_cell(cell) for cell in row) + " |"
            for row in normalized
        ]
        rendered_rows.insert(1, "| " + " | ".join("---" for _ in normalized[0]) + " |")
        provisional.append(
            _MarkdownTable(
                bbox=bbox,
                markdown="\n".join(rendered_rows),
                plain_text="\n".join("\t".join(row) for row in normalized),
                row_count=len(normalized),
                column_count=column_count,
            )
        )

    overlapping: set[int] = set()
    for first_index, first in enumerate(provisional):
        for second_index in range(first_index + 1, len(provisional)):
            if _table_bboxes_overlap(first, provisional[second_index]):
                overlapping.update((first_index, second_index))
    accepted = tuple(
        table for index, table in enumerate(provisional) if index not in overlapping
    )
    rejected += len(overlapping)
    if not accepted and (raw_tables or fallback_evidence):
        rejected = max(1, rejected)
    return _TableExtraction(
        tables=tuple(sorted(accepted, key=lambda table: (table.bbox[1], table.bbox[0]))),
        flattened_candidates=rejected,
        plumber_bbox=plumber_bbox,
    )


def _has_rtl_text(value: str) -> bool:
    return any(unicodedata.bidirectional(character) in {"R", "AL", "AN"} for character in value)


def _ruled_grid_detected(
    horizontal: list[tuple[float, float, float, float]],
    vertical: list[tuple[float, float, float, float]],
    check_cancelled: Callable[[], None] | None,
) -> bool:
    if len(horizontal) < 3 or len(vertical) < 3:
        return False
    intersections = 0
    comparisons = 0
    for h_left, h_bottom, h_right, h_top in horizontal:
        h_y = (h_bottom + h_top) / 2
        for v_left, v_bottom, v_right, v_top in vertical:
            comparisons += 1
            if check_cancelled is not None and comparisons % 1024 == 0:
                check_cancelled()
            v_x = (v_left + v_right) / 2
            if h_left - 2 <= v_x <= h_right + 2 and v_bottom - 2 <= h_y <= v_top + 2:
                intersections += 1
                if intersections >= 9:
                    return True
    return False


def _scan_page_objects(
    page: Any,
    *,
    analyze_layout: bool,
    check_cancelled: Callable[[], None] | None,
) -> _ObjectScan:
    """Bound one object walk used for text-layer and ruled-grid evidence."""

    import pypdfium2.raw as pdfium_c

    has_text_object = False
    horizontal: list[tuple[float, float, float, float]] = []
    vertical: list[tuple[float, float, float, float]] = []
    truncated = False
    table_path_segments = 0
    table_analysis_safe = True
    for index, page_object in enumerate(page.get_objects(max_depth=15)):
        if index >= _MAX_PAGE_OBJECTS_PER_PAGE:
            truncated = True
            break
        if check_cancelled is not None and index % 128 == 0:
            check_cancelled()
        if page_object.type == pdfium_c.FPDF_PAGEOBJ_TEXT:
            has_text_object = True
        if (
            page_object.type == pdfium_c.FPDF_PAGEOBJ_FORM
            and page_object.level >= 14
        ):
            # pypdfium2 stops before children below the fifteenth level. Treat
            # a terminal form as incomplete evidence so a raw-zero page cannot
            # be mislabeled as lacking a text layer.
            truncated = True
        if not analyze_layout or page_object.type != pdfium_c.FPDF_PAGEOBJ_PATH:
            continue
        try:
            segment_count = int(pdfium_c.FPDFPath_CountSegments(page_object.raw))
        except (AttributeError, TypeError, ValueError, OSError):
            table_analysis_safe = False
            segment_count = 0
        if segment_count < 0:
            table_analysis_safe = False
        else:
            table_path_segments += segment_count
            if table_path_segments > _MAX_TABLE_PATH_SEGMENTS_PER_PAGE:
                table_analysis_safe = False
        try:
            left, bottom, right, top = (
                float(value) for value in page_object.get_bounds()
            )
        except (RuntimeError, TypeError, ValueError, OSError):
            continue
        width, height = abs(right - left), abs(top - bottom)
        bounds = (min(left, right), min(bottom, top), max(left, right), max(bottom, top))
        if (
            len(horizontal) < 512
            and width >= 24.0
            and height <= max(3.0, width * 0.03)
        ):
            horizontal.append(bounds)
        if (
            len(vertical) < 512
            and height >= 24.0
            and width <= max(3.0, height * 0.03)
        ):
            vertical.append(bounds)
    return _ObjectScan(
        has_text_object=has_text_object,
        ruled_table=_ruled_grid_detected(horizontal, vertical, check_cancelled),
        truncated=truncated,
        table_analysis_safe=table_analysis_safe and not truncated,
    )


def _extract_page(
    page: Any,
    *,
    markdown: bool,
    tables_requested: bool = False,
    table_page: Any | None = None,
    analyze_layout: bool = True,
    sample_style: bool = True,
    decoded_byte_limit: int | None = None,
    output_byte_limit: int | None = None,
    memory_limit: int | None = None,
    check_cancelled: Callable[[], None] | None = None,
) -> _PageText:
    textpage = page.get_textpage()
    try:
        raw_char_count = textpage.count_chars()
        if decoded_byte_limit is not None and raw_char_count > decoded_byte_limit:
            raise PipelineError(
                f"PDFium reports {raw_char_count:,} characters on this page, over the "
                f"remaining configured {decoded_byte_limit:,}-byte decompressed-text limit"
            )
        if (
            output_byte_limit is not None
            and raw_char_count > output_byte_limit
            and raw_char_count > _MIN_OUTPUT_PREFLIGHT_CHARS
        ):
            raise PipelineError(
                f"PDFium reports {raw_char_count:,} characters on this page, over the "
                f"remaining configured {output_byte_limit:,}-byte output limit"
            )
        if memory_limit is not None:
            conservative_char_limit = memory_limit // 64
            if raw_char_count > conservative_char_limit:
                raise PipelineError(
                    f"PDFium reports {raw_char_count:,} characters on this page, over the "
                    "configured extraction memory preflight"
                )
        object_scan = (
            _ObjectScan(has_text_object=True, ruled_table=False, truncated=False)
            if raw_char_count > 0 and not analyze_layout
            else _scan_page_objects(
                page,
                analyze_layout=analyze_layout,
                check_cancelled=check_cancelled,
            )
        )
        has_text_layer = raw_char_count > 0 or object_scan.has_text_object
        if object_scan.truncated and not has_text_layer:
            raise PipelineError(
                f"[{READING_ORDER_UNCERTAIN}] Page object inventory exceeded "
                f"{_MAX_PAGE_OBJECTS_PER_PAGE:,} objects; refusing to guess whether a text "
                "layer is absent"
            )
        working_byte_limits = [
            limit for limit in (decoded_byte_limit,) if limit is not None
        ]
        if raw_char_count > _MIN_OUTPUT_PREFLIGHT_CHARS and output_byte_limit is not None:
            working_byte_limits.append(output_byte_limit)
        working_byte_limit = min(working_byte_limits) if working_byte_limits else None
        fragments, rectangles_truncated = _extract_fragments(
            page,
            textpage,
            byte_limit=working_byte_limit,
            sample_style=sample_style,
            check_cancelled=check_cancelled,
        )
        page_box = tuple(float(value) for value in page.get_bbox())
        page_width = max(1.0, page_box[2] - page_box[0])
        lines, multiple_columns, possible_unruled_table = _lines_from_fragments(
            fragments, page_width, check_cancelled
        )
        fallback_used = rectangles_truncated
        if not lines:
            bounded = _normalize_text(textpage.get_text_bounded(errors="strict"))
            if (
                working_byte_limit is not None
                and len(bounded.encode("utf-8", errors="strict")) > working_byte_limit
            ):
                raise PipelineError(
                    "PDFium page text exceeds the configured extraction byte limit"
                )
            if bounded:
                fallback_used = True
                fallback_lines = bounded.split("\n")
                lines = [
                    _Line(
                        text=line,
                        left=page_box[0],
                        bottom=float(-index),
                        right=page_box[2],
                        top=float(1 - index),
                        font_size=0.0,
                    )
                    for index, line in enumerate(fallback_lines)
                    if line
                ]

        rotated = bool(page.get_rotation())
        table_detected = object_scan.ruled_table if analyze_layout else False
        table_extraction = _TableExtraction()
        rendered_lines = lines
        column_order_uncertain = multiple_columns and not table_detected
        if markdown and tables_requested:
            if fallback_used:
                table_extraction = _TableExtraction(
                    flattened_candidates=int(table_detected or possible_unruled_table)
                )
            else:
                table_extraction = _extract_markdown_tables(
                    table_page,
                    page_box=page_box,
                    rotated=rotated,
                    object_scan=object_scan,
                    possible_unruled_table=possible_unruled_table,
                    byte_limit=working_byte_limit,
                    memory_limit=memory_limit,
                    check_cancelled=check_cancelled,
                )
                remaining_fragments, table_extraction = _apply_table_regions(
                    fragments,
                    table_extraction,
                    page_box=page_box,
                )
                if table_extraction.tables:
                    rendered_lines, column_order_uncertain, _ = _lines_from_fragments(
                        remaining_fragments, page_width, check_cancelled
                    )

        heading_levels = _heading_levels(rendered_lines) if markdown else {}
        if table_extraction.tables and table_extraction.plumber_bbox is not None:
            plain_text = _normalize_text(
                _render_plain_with_tables(
                    rendered_lines,
                    table_extraction.tables,
                    page_box=page_box,
                    plumber_bbox=table_extraction.plumber_bbox,
                )
            )
            markdown_text = _normalize_text(
                _render_markdown_with_tables(
                    rendered_lines,
                    heading_levels,
                    table_extraction.tables,
                    page_box=page_box,
                    plumber_bbox=table_extraction.plumber_bbox,
                )
            )
        else:
            plain_text = _normalize_text(_render_plain(lines))
            markdown_text = (
                _normalize_text(_render_markdown(lines, heading_levels))
                if markdown
                else plain_text
            )
        angled = any(abs(fragment.angle) > 0.01 for fragment in fragments)
        uncertain = (
            rotated
            or angled
            or _has_rtl_text(plain_text)
            or column_order_uncertain
            or fallback_used
            or object_scan.truncated
        )

        present: set[str] = set()
        if not has_text_layer:
            present.add(NO_TEXT_LAYER)
        if heading_levels:
            present.add(HEADINGS_INFERRED)
        if uncertain:
            present.add(READING_ORDER_UNCERTAIN)
        if table_extraction.tables:
            present.add(TABLE_FIDELITY_BEST_EFFORT)
        if (
            table_extraction.flattened_candidates
            or (table_detected and not tables_requested)
        ):
            present.add(TABLES_FLATTENED)
        return _PageText(
            plain_text=plain_text,
            markdown_text=markdown_text,
            has_text_layer=has_text_layer,
            warning_codes=tuple(code for code in WARNING_CODE_ORDER if code in present),
            emitted_tables=len(table_extraction.tables),
            flattened_table_candidates=(
                table_extraction.flattened_candidates
                if tables_requested
                else int(table_detected)
            ),
        )
    finally:
        textpage.close()


def _sanitize_reserved_markers(value: str) -> str:
    """Keep source marker lookalikes visibly textual, never structural."""

    value = _SOURCE_MD_ANCHOR.sub(
        lambda match: match.group(0).replace("<!--", "&lt;!--", 1), value
    )
    return _SOURCE_TXT_ANCHOR.sub(
        lambda match: match.group(0).replace("---", "--\\-", 1), value
    )


def _write_counted(
    handle: TextIO,
    value: str,
    *,
    current_bytes: int,
    byte_limit: int | None,
) -> int:
    next_bytes = current_bytes + len(value.encode("utf-8", errors="strict"))
    if byte_limit is not None and next_bytes > byte_limit:
        raise PipelineError(
            f"Generated text exceeds the configured {byte_limit:,}-byte output limit"
        )
    handle.write(value)
    return next_bytes


def _coverage(per_page: list[dict[str, object]]) -> dict[str, object]:
    counts: list[int] = []
    for item in per_page:
        count = item["char_count"]
        assert isinstance(count, int)
        counts.append(count)
    return {
        "pages_total": len(per_page),
        "pages_with_text": sum(count > 0 for count in counts),
        "pages_with_text_layer": sum(bool(item["has_text_layer"]) for item in per_page),
        "char_count_min": min(counts),
        "char_count_median": statistics.median(counts),
        "char_count_max": max(counts),
        "per_page": per_page,
    }


def _coverage_shape_valid(
    coverage: dict[str, object], selection: tuple[int, ...]
) -> tuple[bool, str]:
    expected_keys = {
        "pages_total",
        "pages_with_text",
        "pages_with_text_layer",
        "char_count_min",
        "char_count_median",
        "char_count_max",
        "per_page",
    }
    per_page = coverage.get("per_page")
    if (
        set(coverage) != expected_keys
        or not isinstance(per_page, list)
        or type(coverage.get("pages_total")) is not int
        or coverage.get("pages_total") != len(selection)
        or len(per_page) != len(selection)
    ):
        return False, "coverage summary or per-page cardinality is invalid"
    expected_record_keys = {"page", "char_count", "has_text_layer", "warning_codes"}
    counts: list[int] = []
    text_layers = 0
    for page_number, item in zip(selection, per_page, strict=True):
        if not isinstance(item, dict) or set(item) != expected_record_keys:
            return False, "a per-page coverage record has the wrong schema"
        if type(item["page"]) is not int or item["page"] != page_number:
            return False, "per-page coverage order differs from the requested pages"
        if type(item["char_count"]) is not int or item["char_count"] < 0:
            return False, "a per-page character count is invalid"
        if not isinstance(item["has_text_layer"], bool):
            return False, "a per-page text-layer flag is invalid"
        codes = item["warning_codes"]
        if not isinstance(codes, list) or any(
            not isinstance(code, str) or code not in WARNING_CODE_ORDER for code in codes
        ):
            return False, "a per-page warning code is invalid"
        if codes != [code for code in WARNING_CODE_ORDER if code in codes]:
            return False, "per-page warning codes are duplicated or out of stable order"
        counts.append(item["char_count"])
        text_layers += int(item["has_text_layer"])
    expected_summary: dict[str, object] = {
        "pages_total": len(selection),
        "pages_with_text": sum(count > 0 for count in counts),
        "pages_with_text_layer": text_layers,
        "char_count_min": min(counts),
        "char_count_median": statistics.median(counts),
        "char_count_max": max(counts),
    }
    for key, expected in expected_summary.items():
        value = coverage.get(key)
        if type(value) is not type(expected) or value != expected:
            return False, f"coverage field {key!r} disagrees with per-page records"
    return True, f"{len(per_page)} ordered per-page coverage record(s)"


def _text_validator(
    *,
    output_format: str,
    page_anchors: bool,
    selection: tuple[int, ...],
    coverage: dict[str, object],
):
    """Build deterministic semantic validation for one text candidate."""

    def validate(path: Path) -> ValidationResult:
        checks: list[ValidationCheck] = []
        exists = path.is_file()
        checks.append(ValidationCheck(name="file-exists", passed=exists, detail=path.name))
        if not exists:
            return ValidationResult.combine(checks)

        coverage_valid, coverage_detail = _coverage_shape_valid(coverage, selection)
        coverage_records_value = coverage.get("per_page")
        coverage_records = (
            coverage_records_value if isinstance(coverage_records_value, list) else []
        )
        anchors: list[int] = []
        form_feeds = 0
        lf_only = True
        record_count = 0
        jsonl_valid = True
        parse_error = ""
        try:
            with path.open("r", encoding="utf-8", errors="strict", newline="") as handle:
                for raw_line in handle:
                    lf_only = lf_only and "\r" not in raw_line
                    form_feeds += raw_line.count("\f")
                    line = raw_line[:-1] if raw_line.endswith("\n") else raw_line
                    if output_format == "md":
                        match = _MD_ANCHOR.fullmatch(line)
                        if match is not None:
                            anchors.append(int(match.group(1)))
                    elif output_format == "txt":
                        match = _TXT_ANCHOR.fullmatch(line)
                        if match is not None:
                            anchors.append(int(match.group(1)))
                    else:
                        occurrence_index = record_count
                        try:
                            record = json.loads(raw_line)
                        except json.JSONDecodeError as exc:
                            jsonl_valid = False
                            parse_error = parse_error or str(exc)
                            record_count += 1
                            continue
                        page_number = (
                            selection[occurrence_index]
                            if occurrence_index < len(selection)
                            else None
                        )
                        coverage_record = (
                            coverage_records[occurrence_index]
                            if occurrence_index < len(coverage_records)
                            else None
                        )
                        record_count += 1
                        if not isinstance(record, dict) or list(record) != [
                            "page",
                            "text",
                            "char_count",
                            "has_text_layer",
                        ]:
                            jsonl_valid = False
                            continue
                        text = record["text"]
                        if (
                            type(record["page"]) is not int
                            or record["page"] != page_number
                            or not isinstance(text, str)
                            or type(record["char_count"]) is not int
                            or record["char_count"] != len(text)
                            or type(record["has_text_layer"]) is not bool
                            or not isinstance(coverage_record, dict)
                            or record["char_count"] != coverage_record.get("char_count")
                            or record["has_text_layer"]
                            != coverage_record.get("has_text_layer")
                        ):
                            jsonl_valid = False
        except (UnicodeError, OSError) as exc:
            checks.append(
                ValidationCheck(name="utf-8-decodes", passed=False, detail=str(exc))
            )
            return ValidationResult.combine(checks)
        checks.append(
            ValidationCheck(
                name="utf-8-decodes",
                passed=True,
                detail=f"{path.stat().st_size} byte(s), checked as a stream",
            )
        )
        checks.append(
            ValidationCheck(
                name="line-endings-lf",
                passed=lf_only,
                detail="no carriage returns" if lf_only else "carriage return found",
            )
        )
        checks.append(
            ValidationCheck(
                name="coverage-stats-present",
                passed=coverage_valid,
                detail=coverage_detail,
            )
        )

        if output_format == "md":
            expected = selection if page_anchors else ()
            checks.append(
                ValidationCheck(
                    name="page-anchors-exact",
                    passed=tuple(anchors) == expected,
                    detail=f"expected {list(expected)}, found {list(anchors)}",
                )
            )
        elif output_format == "txt":
            expected = selection if page_anchors else ()
            separators_valid = (
                form_feeds == 0 if page_anchors else form_feeds == len(selection) - 1
            )
            checks.extend(
                [
                    ValidationCheck(
                        name="page-anchors-exact",
                        passed=tuple(anchors) == expected,
                        detail=f"expected {list(expected)}, found {list(anchors)}",
                    ),
                    ValidationCheck(
                        name="page-separators-exact",
                        passed=separators_valid,
                        detail=f"found {form_feeds} form-feed separator(s)",
                    ),
                ]
            )
        else:
            checks.append(
                ValidationCheck(
                    name="jsonl-records-exact",
                    passed=jsonl_valid and record_count == len(selection),
                    detail=parse_error or f"{record_count} record(s), checked as a stream",
                )
            )
        return ValidationResult.combine(checks)

    return validate


def _aggregate_warnings(
    per_page: list[dict[str, object]],
    *,
    emitted_tables: int = 0,
    flattened_table_candidates: int = 0,
) -> list[FidelityWarning]:
    affected = dict.fromkeys(WARNING_CODE_ORDER, 0)
    for item in per_page:
        codes = item["warning_codes"]
        assert isinstance(codes, list)
        for code in codes:
            if isinstance(code, str) and code in affected:
                affected[code] += 1
    messages = {
        NO_TEXT_LAYER: (
            "selected page occurrence(s) have no extractable text layer. Use "
            "`ldf pdf-to-images --preset llm` for a visual fallback; OCR is not implemented."
        ),
        HEADINGS_INFERRED: (
            "selected page occurrence(s) used a font-size heuristic to infer Markdown heading "
            "levels; verify structure."
        ),
        READING_ORDER_UNCERTAIN: (
            "selected page occurrence(s) triggered a layout heuristic for rotation, "
            "angled/RTL text, fallback extraction, or multiple columns, so reading order "
            "may be uncertain."
        ),
        TABLE_FIDELITY_BEST_EFFORT: (
            "table(s) were rendered as GFM pipe tables from a conservative line-grid "
            "heuristic; verify headers, cell order, and spanning-cell fidelity."
        ),
        TABLES_FLATTENED: (
            "table candidate(s) from a layout heuristic were kept as flowed text because "
            "table output was disabled or confidence/resource checks refused a rectangular "
            "GFM table."
        ),
    }
    counts = dict(affected)
    if emitted_tables:
        counts[TABLE_FIDELITY_BEST_EFFORT] = emitted_tables
    if flattened_table_candidates:
        counts[TABLES_FLATTENED] = flattened_table_candidates
    return [
        FidelityWarning(code=code, message=f"{counts[code]} {messages[code]}")
        for code in WARNING_CODE_ORDER
        if affected[code]
    ]


def _open_pdfium_document(path: Path, password: str | None):
    import pypdfium2 as pdfium

    return pdfium.PdfDocument(str(path), password=password)


def _open_pdfplumber_document(
    path: Path,
    password: str | None,
    pages: tuple[int, ...],
):
    import pdfplumber

    return pdfplumber.open(
        path,
        password=password,
        pages=sorted(set(pages)),
        unicode_norm="NFC",
    )


def _close_pdfplumber_resource(resource: Any | None) -> None:
    if resource is None:
        return
    try:
        resource.close()
    except Exception:
        # Parser cache/close errors must not leak diagnostics or replace the
        # already-available PDFium fallback.
        return


def pdf_to_md(
    input_path: Path,
    output: Path,
    *,
    options: PdfToMdOptions | None = None,
) -> ConversionReport:
    """Extract selected PDF pages to Markdown, plain text, or JSONL."""

    options = options or PdfToMdOptions()
    output_format = options.output_format.strip().lower()
    if output_format not in TEXT_OUTPUT_FORMATS:
        raise PipelineError(
            f"Unsupported text output format {options.output_format!r}; use md, txt, or jsonl"
        )
    if options.tables and output_format != "md":
        raise PipelineError("Table extraction requires Markdown output; use --format md")

    registry = default_registry()
    engine = registry.engine_for(OP_PDF_TO_MD)
    engine_info = engine.probe()

    def execute(context: JobContext, artifacts: list[InputArtifact]) -> ExecuteResult:
        source = artifacts[0].path
        security: list[SecurityWarning] = []
        with _open_pdf(source, options.password) as parsed:
            encrypted = parsed.is_encrypted
        if encrypted:
            security.append(
                SecurityWarning(
                    code="input-encryption-removed",
                    message=(
                        "The input PDF was password protected. Extracted text output is not "
                        "password protected and must be secured separately."
                    ),
                    severity=WarningSeverity.CRITICAL,
                )
            )

        pdf = _open_pdfium_document(source, options.password)
        per_page: list[dict[str, object]] = []
        extracted_bytes = 0
        emitted_tables = 0
        flattened_table_candidates = 0
        table_document: Any | None = None
        table_pages: dict[int, Any] = {}
        table_engine_status = "not-requested"
        staging = context.workspace / f"document.{output_format}"
        try:
            total = len(pdf)
            page_limit = context.limits.max_pages
            if page_limit is not None and total > page_limit:
                raise PipelineError(
                    f"Input has {total} pages after opening, over the configured limit of "
                    f"{page_limit}"
                )
            selection = (options.pages or PageRange(spec="all")).resolve(total)
            if page_limit is not None and len(selection) > page_limit:
                raise PipelineError(
                    f"Requested output has {len(selection)} pages, over the configured limit "
                    f"of {page_limit}"
                )
            if options.tables:
                table_engine_status = "available"
                try:
                    table_document = _open_pdfplumber_document(
                        source,
                        options.password,
                        selection,
                    )
                    table_pages = {
                        int(item.page_number): item for item in table_document.pages
                    }
                except Exception:
                    table_engine_status = "fallback"
                    _close_pdfplumber_resource(table_document)
                    table_document = None
                    table_pages = {}

            written_bytes = 0
            with staging.open("w", encoding="utf-8", errors="strict", newline="\n") as handle:
                for order, page_number in enumerate(selection):
                    context.emit(
                        "extract-text",
                        current=order,
                        total=len(selection),
                        message=f"page {page_number}",
                    )
                    page = pdf[page_number - 1]
                    table_page = table_pages.get(page_number)
                    try:
                        decompressed_limit = context.limits.max_decompressed_bytes
                        output_limit = context.limits.max_output_bytes
                        remaining_decompressed = (
                            None
                            if decompressed_limit is None
                            else max(0, decompressed_limit - extracted_bytes)
                        )
                        remaining_output = (
                            None
                            if output_limit is None
                            else max(0, output_limit - written_bytes)
                        )
                        page_text = _extract_page(
                            page,
                            markdown=output_format == "md",
                            tables_requested=options.tables,
                            table_page=table_page,
                            decoded_byte_limit=remaining_decompressed,
                            output_byte_limit=remaining_output,
                            memory_limit=context.limits.max_memory_bytes,
                            check_cancelled=context.check_cancelled,
                        )
                    finally:
                        page.close()
                        _close_pdfplumber_resource(table_page)

                    extracted_bytes += len(page_text.plain_text.encode("utf-8", errors="strict"))
                    emitted_tables += page_text.emitted_tables
                    flattened_table_candidates += page_text.flattened_table_candidates
                    if decompressed_limit is not None and extracted_bytes > decompressed_limit:
                        raise PipelineError(
                            "Extracted text exceeds the configured "
                            f"{decompressed_limit:,}-byte decompressed limit"
                        )
                    per_page.append(
                        {
                            "page": page_number,
                            "char_count": page_text.char_count,
                            "has_text_layer": page_text.has_text_layer,
                            "warning_codes": list(page_text.warning_codes),
                        }
                    )

                    if output_format == "jsonl":
                        chunk = json.dumps(
                            {
                                "page": page_number,
                                "text": page_text.plain_text,
                                "char_count": page_text.char_count,
                                "has_text_layer": page_text.has_text_layer,
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ) + "\n"
                    else:
                        content = (
                            page_text.markdown_text
                            if output_format == "md"
                            else page_text.plain_text
                        )
                        content = _sanitize_reserved_markers(content)
                        if options.page_anchors:
                            anchor = (
                                f"<!-- ldf:page {page_number} -->"
                                if output_format == "md"
                                else f"--- ldf:page {page_number} ---"
                            )
                            body = (
                                ("\n\n" if output_format == "md" else "\n") + content
                                if content
                                else ""
                            )
                            content = anchor + body
                            separator = "\n\n" if order else ""
                        else:
                            separator = (
                                ("\n\n" if output_format == "md" else "\n\f\n")
                                if order
                                else ""
                            )
                        chunk = separator + content
                    written_bytes = _write_counted(
                        handle,
                        chunk,
                        current_bytes=written_bytes,
                        byte_limit=output_limit,
                    )
        finally:
            _close_pdfplumber_resource(table_document)
            pdf.close()

        coverage = _coverage(per_page)
        effective_anchors = options.page_anchors if output_format in {"md", "txt"} else False
        return ExecuteResult(
            candidates=[
                CandidateOutput(
                    workspace_path=staging,
                    destination=output,
                    media_type=_MEDIA_TYPES[output_format],
                    validator=_text_validator(
                        output_format=output_format,
                        page_anchors=effective_anchors,
                        selection=selection,
                        coverage=coverage,
                    ),
                )
            ],
            fidelity_warnings=_aggregate_warnings(
                per_page,
                emitted_tables=emitted_tables,
                flattened_table_candidates=flattened_table_candidates,
            ),
            security_warnings=security,
            output_page_count=len(selection),
            details={
                "format": output_format,
                "pages_spec": (options.pages or PageRange(spec="all")).spec,
                "page_anchors": effective_anchors,
                "tables": {
                    "requested": options.tables,
                    "engine_status": table_engine_status,
                    "emitted": emitted_tables,
                    "flattened_candidates": flattened_table_candidates,
                },
                "coverage": coverage,
                "heuristics": {
                    "headings": "font-size comparison",
                    "reading_order": "user-space top-to-bottom, left-to-right geometry",
                    "tables": (
                        "pdfplumber line grids with bounded rectangular confidence checks"
                        if options.tables
                        else "intersecting thin ruled path objects (flowed text only)"
                    ),
                },
            },
        )

    return run_pipeline(
        operation=OP_PDF_TO_MD,
        input_paths=[input_path],
        execute=execute,
        engine_name=engine.name,
        engine_version=engine_info.version,
        collision=options.collision,
        settings=options.settings,
        progress=options.progress,
    )


def inspect_page_text_stats(
    input_path: Path,
    *,
    password: str | None = None,
    limits: ResourceLimits | None = None,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    """Return per-page PDFium text coverage without retaining page text."""

    limits = limits or ResourceLimits()
    pdf = _open_pdfium_document(input_path, password)
    per_page: list[dict[str, object]] = []
    decoded_bytes = 0
    try:
        page_count = len(pdf)
        if limits.max_pages is not None and page_count > limits.max_pages:
            raise PipelineError(
                f"Input has {page_count} pages, over the configured limit "
                f"of {limits.max_pages}"
            )
        for index in range(page_count):
            decoded_limit = (
                max(0, limits.max_decompressed_bytes - decoded_bytes)
                if limits.max_decompressed_bytes is not None
                else None
            )
            page = pdf[index]
            try:
                page_text = _extract_page(
                    page,
                    markdown=False,
                    analyze_layout=False,
                    sample_style=False,
                    decoded_byte_limit=decoded_limit,
                    memory_limit=limits.max_memory_bytes,
                )
            finally:
                page.close()
            decoded_bytes += len(page_text.plain_text.encode("utf-8", errors="strict"))
            if (
                limits.max_decompressed_bytes is not None
                and decoded_bytes > limits.max_decompressed_bytes
            ):
                raise PipelineError(
                    "Extracted text exceeds the configured "
                    f"{limits.max_decompressed_bytes:,}-byte decompressed limit"
                )
            per_page.append(
                {
                    "page": index + 1,
                    "char_count": page_text.char_count,
                    "has_text_layer": page_text.has_text_layer,
                }
            )
    finally:
        pdf.close()
    counts: list[int] = []
    for item in per_page:
        count = item["char_count"]
        assert isinstance(count, int)
        counts.append(count)
    summary: dict[str, object] = {
        "pages_total": len(per_page),
        "pages_with_text": sum(count > 0 for count in counts),
        "pages_with_text_layer": sum(bool(item["has_text_layer"]) for item in per_page),
        "char_count_min": min(counts) if counts else None,
        "char_count_median": statistics.median(counts) if counts else None,
        "char_count_max": max(counts) if counts else None,
    }
    return per_page, summary
