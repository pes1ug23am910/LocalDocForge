"""Render a bounded CommonMark subset to validated PDF through Typst.

The Markdown parser never emits Typst markup directly. Every untrusted text,
code, link, and alt-text value becomes a canonical Typst string literal, while
local images are validated as pipeline inputs and normalized to neutral PNGs
inside the private job workspace. Typst therefore sees only generated source
and copied assets beneath its explicit ``--root``.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import warnings
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt
from markdown_it.token import Token

from localdocforge.config.settings import Settings, get_settings
from localdocforge.domain.models import (
    ConversionReport,
    FidelityWarning,
    InputArtifact,
    JobCancelled,
    JobContext,
    ProgressCallback,
    WarningSeverity,
)
from localdocforge.engines.adapters import OP_MD_TO_PDF
from localdocforge.engines.registry import default_registry
from localdocforge.jobs.workspace import CollisionPolicy
from localdocforge.operations import images as image_ops
from localdocforge.pipelines.runner import (
    CandidateOutput,
    ExecuteResult,
    PipelineError,
    run_pipeline,
)
from localdocforge.security.paths import (
    PathSecurityError,
    ensure_contained,
    validate_path_before_access,
)
from localdocforge.security.sniff import ContentTypeError, require_media_type
from localdocforge.security.subproc import ToolError, ToolTimeout, run_tool
from localdocforge.validation.pdf_checks import count_pdf_pages

MARKDOWN_MEDIA_TYPE = "text/markdown"
MARKDOWN_CONSTRUCT_DROPPED = "markdown-construct-dropped"
SYSTEM_FONT_DEPENDENT = "system-font-dependent"

_MAX_MARKDOWN_NESTING = 64
_MAX_MARKDOWN_SOURCE_BYTES = 16 * 1024 * 1024
_MAX_MARKDOWN_SOURCE_LINES = 100_000
_MAX_MARKDOWN_TOKENS = 250_000
_MAX_MARKDOWN_IMAGE_REFERENCES = 256
_MAX_REPORTED_DROPPED_CONSTRUCTS = 256
_MARKDOWN_MEMORY_EXPANSION_FACTOR = 512
_MARKDOWN_TEMPORARY_EXPANSION_FACTOR = 64
_MAX_TYPST_TIMEOUT_SECONDS = 600.0
_MAX_TOOL_OUTPUT_BYTES = 64 * 1024
_MAX_DEPENDENCY_MANIFEST_BYTES = 1024 * 1024
_ALLOWED_LINK_SCHEMES = frozenset({"http", "https", "mailto", "tel"})
_FORBIDDEN_GENERATED_SOURCE = (
    "#eval",
    "#read",
    "#import",
    "#include",
    "#plugin",
    "@preview",
)
_PAPERS_MM: dict[str, tuple[str, float, float]] = {
    "a4": ("A4", 210.0, 297.0),
    "letter": ("Letter", 215.9, 279.4),
    "legal": ("Legal", 215.9, 355.6),
}
_IMAGE_INPUT_TYPES = tuple(image_ops.IMAGE_MEDIA_TYPES)
_FOOTNOTE_DEFINITION = re.compile(r"^ {0,3}\[\^[^\]\r\n]+\]:")
_FOOTNOTE_REFERENCE = re.compile(r"\[\^[^\]\r\n]+\]")
_INLINE_DOLLAR_MATH = re.compile(r"(?<!\\)\$(?!\s|\$)(.+?)(?<!\s|\\)\$")
_INLINE_PAREN_MATH = re.compile(r"(?<!\\)\\\((.+?)(?<!\\)\\\)")
_PYTHON_SPLITLINE_SINGLETONS = frozenset(
    {"\n", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"}
)


@dataclass
class MdToPdfOptions:
    paper: str = "A4"
    margin_mm: float = 20.0
    toc: bool = False
    collision: CollisionPolicy | None = None
    settings: Settings | None = None
    progress: ProgressCallback | None = None


@dataclass(frozen=True, order=True)
class _DroppedConstruct:
    line: int
    construct: str


class _DropTracker:
    def __init__(self) -> None:
        self._items: set[_DroppedConstruct] = set()
        self._omitted = 0
        self._first_omitted_line: int | None = None

    def add(self, construct: str, line: int) -> None:
        item = _DroppedConstruct(max(1, line), construct)
        if item in self._items:
            return
        if len(self._items) < _MAX_REPORTED_DROPPED_CONSTRUCTS:
            self._items.add(item)
            return
        self._omitted += 1
        if self._first_omitted_line is None:
            self._first_omitted_line = item.line

    @property
    def items(self) -> list[_DroppedConstruct]:
        items = sorted(self._items)
        if self._omitted:
            items.append(
                _DroppedConstruct(
                    self._first_omitted_line or 1,
                    (
                        "additional unsupported constructs "
                        f"({self._omitted} occurrence(s) omitted after the "
                        f"{_MAX_REPORTED_DROPPED_CONSTRUCTS}-entry report cap)"
                    ),
                )
            )
        return items

    @property
    def omitted(self) -> int:
        return self._omitted


@dataclass(frozen=True)
class _ImageReference:
    target: str
    alt: str
    line: int


@dataclass(frozen=True)
class _Asset:
    source: Path
    workspace_name: str
    first_line: int


@dataclass(frozen=True)
class _MarkdownSnapshot:
    path: Path
    source: str
    digest: bytes
    size_bytes: int
    byte_limit: int


@dataclass
class _Node:
    token: Token
    children: list[_Node] = field(default_factory=list)

    @property
    def kind(self) -> str:
        return self.token.type.removesuffix("_open")


def normalize_paper(value: str) -> tuple[str, float, float]:
    key = value.strip().casefold()
    try:
        return _PAPERS_MM[key]
    except KeyError:
        available = ", ".join(item[0] for item in _PAPERS_MM.values())
        raise PipelineError(f"Unknown paper size {value!r}; use {available}") from None


def validate_margin(margin_mm: float, paper: tuple[str, float, float]) -> None:
    if not math.isfinite(margin_mm) or margin_mm < 0:
        raise PipelineError("Margin must be a finite, non-negative number of millimetres")
    if margin_mm * 2 >= min(paper[1], paper[2]):
        raise PipelineError(f"Margin leaves no drawable area on {paper[0]} paper")


def _markdown_parser() -> MarkdownIt:
    parser = MarkdownIt(
        "commonmark",
        {
            "html": True,
            "linkify": False,
            "typographer": False,
            "maxNesting": _MAX_MARKDOWN_NESTING,
        },
    )
    parser.enable("table")
    parser.enable("strikethrough")
    return parser


def _blank_line_like(value: str) -> str:
    if value.endswith("\r\n"):
        return "\r\n"
    if value.endswith(("\n", "\r")):
        return value[-1]
    return ""


def _enforce_token_limit(tokens: list[Token]) -> None:
    count = 0
    pending = list(tokens)
    while pending:
        token = pending.pop()
        count += 1
        if count > _MAX_MARKDOWN_TOKENS:
            raise PipelineError(
                "Markdown token stream exceeds the configured safe preprocessing limit"
            )
        if token.children:
            pending.extend(token.children)


def _protected_code_ranges(source: str) -> list[tuple[int, int]]:
    parser = MarkdownIt("commonmark", {"maxNesting": _MAX_MARKDOWN_NESTING})
    tokens = parser.parse(source)
    _enforce_token_limit(tokens)
    protected: list[tuple[int, int]] = []
    for token in tokens:
        if token.type not in {"fence", "code_block"} or token.map is None:
            continue
        protected.append((token.map[0], token.map[1]))
    return protected


def _drop_block_unsupported(source: str, dropped: _DropTracker) -> str:
    """Blank footnote definitions and math blocks while preserving line numbers."""
    lines = source.splitlines(keepends=True)
    protected = _protected_code_ranges(source)
    protected_index = 0

    def is_protected(line_index: int) -> bool:
        nonlocal protected_index
        while (
            protected_index < len(protected)
            and line_index >= protected[protected_index][1]
        ):
            protected_index += 1
        return (
            protected_index < len(protected)
            and protected[protected_index][0] <= line_index < protected[protected_index][1]
        )

    index = 0
    while index < len(lines):
        if is_protected(index):
            index += 1
            continue
        line = lines[index]
        stripped = line.strip()
        if _FOOTNOTE_DEFINITION.match(line):
            dropped.add("footnote definition", index + 1)
            lines[index] = _blank_line_like(line)
            index += 1
            while index < len(lines) and not is_protected(index):
                continuation = lines[index]
                if continuation.strip() and not continuation.startswith(("    ", "\t")):
                    break
                lines[index] = _blank_line_like(continuation)
                index += 1
            continue

        marker: str | None = None
        closing: str | None = None
        if stripped.startswith("$$"):
            marker, closing = "math block", "$$"
        elif stripped.startswith("\\["):
            marker, closing = "math block", "\\]"
        if marker is not None and closing is not None:
            dropped.add(marker, index + 1)
            remainder = stripped[2:]
            lines[index] = _blank_line_like(line)
            closed = closing in remainder
            index += 1
            while index < len(lines) and not closed:
                candidate = lines[index]
                if not is_protected(index) and closing in candidate:
                    closed = True
                lines[index] = _blank_line_like(candidate)
                index += 1
            continue
        index += 1
    return "".join(lines)


def _build_tree(tokens: list[Token]) -> list[_Node]:
    roots: list[_Node] = []
    child_stacks: list[list[_Node]] = [roots]
    open_nodes: list[_Node] = []
    for token in tokens:
        if token.nesting == 1:
            node = _Node(token)
            child_stacks[-1].append(node)
            child_stacks.append(node.children)
            open_nodes.append(node)
        elif token.nesting == -1:
            if not open_nodes:
                raise PipelineError("Markdown parser produced an unbalanced token stream")
            opened = open_nodes.pop()
            expected = f"{opened.kind}_close"
            if token.type != expected:
                raise PipelineError("Markdown parser produced a mismatched token stream")
            child_stacks.pop()
        else:
            child_stacks[-1].append(_Node(token))
    if open_nodes:
        raise PipelineError("Markdown parser produced an unclosed token stream")
    return roots


def _typst_string(value: str) -> str:
    """Return a Typst string with controls and all ASCII punctuation inert."""
    encoded: list[str] = ['"']
    for character in value:
        codepoint = ord(character)
        if character == "\n":
            encoded.append("\\n")
        elif character == "\r":
            encoded.append("\\r")
        elif character == "\t":
            encoded.append("\\t")
        elif codepoint < 0x20 or codepoint == 0x7F or (
            codepoint < 0x80 and not character.isalnum() and not character.isspace()
        ):
            encoded.append(f"\\u{{{codepoint:x}}}")
        else:
            encoded.append(character)
    encoded.append('"')
    return "".join(encoded)


def _image_references(tokens: list[Token]) -> list[_ImageReference]:
    references: list[_ImageReference] = []
    for token in tokens:
        if token.type != "inline" or not token.children:
            continue
        line = (token.map[0] + 1) if token.map else 1
        for child in token.children:
            if child.type != "image":
                continue
            target = child.attrGet("src") or ""
            references.append(_ImageReference(target=target, alt=child.content, line=line))
    return references


def _decode_local_image_target(target: str, *, line: int) -> str:
    if any(ord(character) < 0x20 for character in target):
        raise PipelineError(f"Markdown image at line {line} has an invalid control character")
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc or target.startswith(("//", "\\\\")):
        raise PipelineError(
            f"Markdown image at line {line} is not a local relative path; remote, data, "
            "file, UNC, and absolute image references are refused"
        )
    if parsed.query or parsed.fragment:
        raise PipelineError(f"Markdown image at line {line} cannot contain a query or fragment")
    try:
        decoded = unquote(parsed.path, encoding="utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise PipelineError(f"Markdown image at line {line} has invalid UTF-8 escaping") from exc
    candidate = Path(decoded)
    if not decoded or candidate.is_absolute() or candidate.drive:
        raise PipelineError(f"Markdown image at line {line} must be a non-empty relative path")
    return decoded


def _markdown_source_budget(settings: Settings) -> int:
    limits = [_MAX_MARKDOWN_SOURCE_BYTES]
    configured_input = settings.limits.max_input_bytes
    if configured_input is not None:
        limits.append(configured_input)
    configured_memory = settings.limits.max_memory_bytes
    if configured_memory is not None:
        limits.append(configured_memory // _MARKDOWN_MEMORY_EXPANSION_FACTOR)
    configured_temporary = settings.limits.max_temporary_bytes
    if configured_temporary is not None:
        limits.append(configured_temporary // _MARKDOWN_TEMPORARY_EXPANSION_FACTOR)
    return min(limits)


def _source_line_count(source: str) -> int:
    if not source:
        return 0
    breaks = 0
    index = 0
    while index < len(source):
        character = source[index]
        if character == "\r":
            breaks += 1
            if index + 1 < len(source) and source[index + 1] == "\n":
                index += 1
        elif character in _PYTHON_SPLITLINE_SINGLETONS:
            breaks += 1
        index += 1
    return breaks + 1


def _load_markdown_source(path: Path, settings: Settings) -> _MarkdownSnapshot:
    try:
        checked = validate_path_before_access(
            path,
            what="Markdown input",
            require_local=settings.strict_offline,
        )
        size = checked.stat().st_size
        limit = _markdown_source_budget(settings)
        if size > limit:
            raise PipelineError(
                f"Markdown input is {size:,} bytes, over the safe preprocessing "
                f"limit of {limit:,} bytes derived from the configured input, memory, "
                "and temporary-byte bounds"
            )
        require_media_type(
            checked,
            MARKDOWN_MEDIA_TYPE,
            max_text_bytes=limit,
        )
        with checked.open("rb") as stream:
            payload = stream.read(limit + 1)
        if len(payload) > limit:
            raise PipelineError(
                "Markdown input grew beyond its safe preprocessing limit while it was read"
            )
    except PipelineError:
        raise
    except (ContentTypeError, FileNotFoundError, OSError, PathSecurityError) as exc:
        raise PipelineError(str(exc)) from exc
    try:
        source = payload.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise PipelineError("Markdown input must be valid UTF-8") from exc
    if "\x00" in source:
        raise PipelineError("Markdown input contains a NUL byte and was refused as binary data")
    line_count = _source_line_count(source)
    if line_count > _MAX_MARKDOWN_SOURCE_LINES:
        raise PipelineError(
            f"Markdown input has {line_count:,} lines, over the safe preprocessing "
            f"limit of {_MAX_MARKDOWN_SOURCE_LINES:,}"
        )
    return _MarkdownSnapshot(
        path=checked.resolve(strict=True),
        source=source,
        digest=hashlib.sha256(payload).digest(),
        size_bytes=len(payload),
        byte_limit=limit,
    )


def _enforce_image_reference_limit(
    references: list[_ImageReference],
) -> None:
    if len(references) > _MAX_MARKDOWN_IMAGE_REFERENCES:
        raise PipelineError(
            f"Markdown has {len(references):,} image references, over the safe "
            f"preprocessing limit of {_MAX_MARKDOWN_IMAGE_REFERENCES:,}"
        )


def _verify_markdown_snapshot(
    snapshot: _MarkdownSnapshot,
    artifact: InputArtifact,
    *,
    settings: Settings,
) -> None:
    try:
        checked = validate_path_before_access(
            snapshot.path,
            what="Markdown input",
            require_local=settings.strict_offline,
        ).resolve(strict=True)
        if (
            not _same_existing_file(checked, artifact.path)
            or artifact.size_bytes != snapshot.size_bytes
            or checked.stat().st_size != snapshot.size_bytes
        ):
            raise OSError("Markdown input identity changed")
        with checked.open("rb") as stream:
            payload = stream.read(snapshot.byte_limit + 1)
        if (
            len(payload) != snapshot.size_bytes
            or hashlib.sha256(payload).digest() != snapshot.digest
        ):
            raise OSError("Markdown input content changed")
    except (FileNotFoundError, OSError, PathSecurityError) as exc:
        raise PipelineError("Markdown input changed after preprocessing") from exc


def _resolve_assets(
    markdown_path: Path,
    references: list[_ImageReference],
    *,
    settings: Settings,
    image_inputs: dict[str, Path] | None,
) -> tuple[list[_Asset], dict[str, str]]:
    assets: list[_Asset] = []
    workspace_by_target: dict[str, str] = {}
    asset_by_source: dict[Path, _Asset] = {}
    used_api_names: set[str] = set()
    document_root = markdown_path.parent.resolve(strict=True)

    for reference in references:
        decoded = _decode_local_image_target(reference.target, line=reference.line)
        try:
            if image_inputs is None:
                candidate = ensure_contained(
                    document_root / decoded,
                    document_root,
                    what=f"Markdown image at line {reference.line}",
                )
            else:
                if Path(decoded).name != decoded or "/" in decoded or "\\" in decoded:
                    raise PipelineError(
                        f"API Markdown image at line {reference.line} must name a sibling upload"
                    )
                mapped = image_inputs.get(decoded)
                if mapped is None:
                    raise PipelineError(
                        f"Markdown image at line {reference.line} has no matching uploaded asset"
                    )
                used_api_names.add(decoded)
                candidate = validate_path_before_access(
                    mapped,
                    what=f"Markdown image at line {reference.line}",
                    require_local=settings.strict_offline,
                ).resolve(strict=True)
            candidate = validate_path_before_access(
                candidate,
                what=f"Markdown image at line {reference.line}",
                require_local=settings.strict_offline,
            ).resolve(strict=True)
            if not candidate.is_file():
                raise FileNotFoundError(candidate)
        except PipelineError:
            raise
        except (FileNotFoundError, OSError, PathSecurityError) as exc:
            raise PipelineError(
                f"Markdown image at line {reference.line} was not found inside its allowed root"
            ) from exc

        asset = asset_by_source.get(candidate)
        if asset is None:
            asset = _Asset(
                source=candidate,
                workspace_name=f"assets/image-{len(assets) + 1:04d}.png",
                first_line=reference.line,
            )
            asset_by_source[candidate] = asset
            assets.append(asset)
        workspace_by_target[reference.target] = asset.workspace_name

    if image_inputs is not None:
        unused = sorted(set(image_inputs) - used_api_names)
        if unused:
            raise PipelineError(
                "Every uploaded Markdown image asset must be referenced by a sibling basename"
            )
    return assets, workspace_by_target


class _TypstRenderer:
    def __init__(
        self,
        *,
        workspace_by_image_target: dict[str, str],
        dropped: _DropTracker,
    ) -> None:
        self._workspace_by_image_target = workspace_by_image_target
        self._dropped = dropped

    @property
    def dropped(self) -> list[_DroppedConstruct]:
        return self._dropped.items

    @property
    def dropped_omitted(self) -> int:
        return self._dropped.omitted

    def _drop(self, construct: str, line: int) -> None:
        self._dropped.add(construct, line)

    def _strip_inline_unsupported(self, value: str, line: int) -> str:
        def remove_footnote(_match: re.Match[str]) -> str:
            self._drop("footnote reference", line)
            return ""

        def remove_math(_match: re.Match[str]) -> str:
            self._drop("inline math", line)
            return ""

        value = _FOOTNOTE_REFERENCE.sub(remove_footnote, value)
        value = _INLINE_DOLLAR_MATH.sub(remove_math, value)
        return _INLINE_PAREN_MATH.sub(remove_math, value)

    def _inline_nodes(self, token: Token) -> list[_Node]:
        return _build_tree(list(token.children or []))

    def _render_inline_nodes(self, nodes: list[_Node], line_state: list[int]) -> str:
        parts: list[str] = []
        for node in nodes:
            token = node.token
            kind = node.kind
            line = line_state[0]
            if kind == "text":
                value = self._strip_inline_unsupported(token.content, line)
                if value:
                    parts.append(f"text({_typst_string(value)})")
            elif kind == "code_inline":
                parts.append(f"raw({_typst_string(token.content)})")
            elif kind == "em":
                body = self._render_inline_nodes(node.children, line_state)
                parts.append(f"emph({body})")
            elif kind == "strong":
                body = self._render_inline_nodes(node.children, line_state)
                parts.append(f"strong({body})")
            elif kind == "link":
                body = self._render_inline_nodes(node.children, line_state)
                destination = token.attrGet("href") or ""
                parsed = urlsplit(destination)
                if (
                    not destination
                    or destination.startswith("//")
                    or parsed.scheme.casefold() not in _ALLOWED_LINK_SCHEMES
                ):
                    self._drop("unsafe or local link destination", line)
                    parts.append(body)
                else:
                    parts.append(f"link({_typst_string(destination)}, {body})")
            elif kind == "image":
                workspace_name = self._workspace_by_image_target.get(token.attrGet("src") or "")
                if workspace_name is None:
                    raise PipelineError("Markdown image mapping changed during rendering")
                parts.append(
                    "box(width: 100%, image("
                    f"{_typst_string(workspace_name)}, "
                    f"alt: {_typst_string(token.content)}, width: 100%"
                    "))"
                )
            elif kind == "softbreak":
                parts.append('text(" ")')
                line_state[0] += 1
            elif kind == "hardbreak":
                parts.append("linebreak()")
                line_state[0] += 1
            elif kind == "html_inline":
                self._drop("raw HTML", line)
            elif kind == "s":
                self._drop("strikethrough", line)
                parts.append(self._render_inline_nodes(node.children, line_state))
            else:
                self._drop(f"token {kind}", line)
                if node.children:
                    parts.append(self._render_inline_nodes(node.children, line_state))
        return " + ".join(part for part in parts if part) or 'text("")'

    def _inline(self, node: _Node) -> str:
        line = (node.token.map[0] + 1) if node.token.map else 1
        return self._render_inline_nodes(self._inline_nodes(node.token), [line])

    def _node_line(self, node: _Node) -> int:
        return (node.token.map[0] + 1) if node.token.map else 1

    def _children_as_stack(self, children: list[_Node]) -> str:
        rendered = [self._block_expression(child) for child in children]
        expressions = [value for value in rendered if value]
        if not expressions:
            return 'text("")'
        if len(expressions) == 1:
            return expressions[0]
        return f"stack(spacing: 0.55em, {', '.join(expressions)})"

    def _table_expression(self, node: _Node) -> str:
        rows: list[tuple[bool, list[str]]] = []

        def visit(item: _Node, header: bool = False) -> None:
            current_header = header or item.kind == "thead"
            if item.kind == "tr":
                cells: list[str] = []
                row_header = current_header
                for cell in item.children:
                    if cell.kind not in {"th", "td"}:
                        continue
                    row_header = row_header or cell.kind == "th"
                    cells.append(self._children_as_stack(cell.children))
                if cells:
                    rows.append((row_header, cells))
                return
            for child in item.children:
                visit(child, current_header)

        visit(node)
        columns = max((len(cells) for _header, cells in rows), default=0)
        if columns < 1:
            self._drop("empty GFM table", self._node_line(node))
            return ""
        arguments: list[str] = []
        for header, cells in rows:
            padded = [*cells, *(['text("")'] * (columns - len(cells)))]
            if header:
                arguments.extend(f"strong({cell})" for cell in padded)
            else:
                arguments.extend(padded)
        return (
            "table("
            f"columns: {columns}, inset: 5pt, stroke: 0.5pt + luma(210), "
            f"{', '.join(arguments)}"
            ")"
        )

    def _block_expression(self, node: _Node) -> str:
        kind = node.kind
        line = self._node_line(node)
        if kind == "inline":
            return self._inline(node)
        if kind == "paragraph":
            return f"par({self._children_as_stack(node.children)})"
        if kind == "heading":
            try:
                level = int(node.token.tag.removeprefix("h"))
            except ValueError:
                level = 1
            heading_body = self._children_as_stack(node.children)
            return f"heading(level: {min(6, max(1, level))}, {heading_body})"
        if kind in {"bullet_list", "ordered_list"}:
            items = [
                self._children_as_stack(child.children)
                for child in node.children
                if child.kind == "list_item"
            ]
            function = "list" if kind == "bullet_list" else "enum"
            options = ["tight: false"]
            if kind == "ordered_list":
                start = node.token.attrGet("start")
                if start is not None and str(start).isdecimal():
                    options.append(f"start: {int(start)}")
            return f"{function}({', '.join([*items, *options])})"
        if kind == "list_item":
            return self._children_as_stack(node.children)
        if kind == "blockquote":
            return f"quote(block: true, quotes: false, {self._children_as_stack(node.children)})"
        if kind in {"fence", "code_block"}:
            return f"raw({_typst_string(node.token.content)}, block: true)"
        if kind == "table":
            return self._table_expression(node)
        if kind in {"thead", "tbody", "tr", "th", "td"}:
            return self._children_as_stack(node.children)
        if kind == "hr":
            return "line(length: 100%, stroke: 0.5pt + luma(170))"
        if kind in {"html_block", "html_inline"}:
            self._drop("raw HTML", line)
            return ""
        self._drop(f"token {kind}", line)
        return self._children_as_stack(node.children) if node.children else ""

    def render(self, nodes: list[_Node]) -> str:
        blocks = [self._block_expression(node) for node in nodes]
        return "\n\n".join(f"#{block}" for block in blocks if block)


def _typst_document(
    body: str,
    *,
    paper: tuple[str, float, float],
    margin_mm: float,
    toc: bool,
) -> str:
    toc_source = "#outline(title: [Contents])\n#pagebreak()\n\n" if toc else ""
    source = (
        f"#set page(width: {paper[1]:g}mm, height: {paper[2]:g}mm, margin: {margin_mm:g}mm)\n"
        "#set text(size: 10.5pt)\n"
        "#set par(leading: 0.68em)\n"
        "#show raw.where(block: true): it => block("
        "width: 100%, fill: luma(246), inset: 8pt, radius: 3pt, it)\n\n"
        f"{toc_source}{body}\n"
    )
    forbidden = [item for item in _FORBIDDEN_GENERATED_SOURCE if item in source.casefold()]
    if forbidden:
        raise PipelineError("Generated Typst source failed the static no-code-injection audit")
    return source


def _normalize_asset(
    asset: _Asset,
    destination: Path,
    *,
    source_stream: BinaryIO,
    media_type: str,
    max_pixels: int | None,
    remaining_decompressed_bytes: int | None,
    decompressed_limit: int | None,
) -> int:
    from PIL import Image, ImageOps

    if media_type == "image/heif":
        image_ops._ensure_heif_opener()
    previous_limit = Image.MAX_IMAGE_PIXELS
    # Retain Pillow's decompression-bomb hard stop while enforcing the exact
    # configured threshold ourselves before EXIF transposition can load pixels.
    Image.MAX_IMAGE_PIXELS = max_pixels
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            source_stream.seek(0)
            with Image.open(source_stream) as image:
                frames = int(getattr(image, "n_frames", 1))
                if frames != 1:
                    raise PipelineError(
                        f"Markdown image at line {asset.first_line} has {frames} frames; "
                        "only single-frame local images are supported"
                    )
                pixel_count = image.width * image.height
                if max_pixels is not None and pixel_count > max_pixels:
                    raise PipelineError(
                        f"Markdown image at line {asset.first_line} has "
                        f"{pixel_count:,} pixels, over the configured "
                        f"{max_pixels:,}-pixel limit"
                    )
                has_alpha = "A" in image.getbands() or "transparency" in image.info
                expected_decompressed_bytes = pixel_count * (4 if has_alpha else 3)
                if (
                    remaining_decompressed_bytes is not None
                    and expected_decompressed_bytes > remaining_decompressed_bytes
                ):
                    raise PipelineError(
                        "Decoded Markdown images exceed the configured "
                        f"{decompressed_limit or 0:,}-byte limit"
                    )
                corrected = ImageOps.exif_transpose(image)
                normalized = None
                try:
                    corrected.load()
                    normalized = corrected.convert("RGBA" if has_alpha else "RGB")
                    bands = len(normalized.getbands())
                    decompressed_bytes = normalized.width * normalized.height * bands
                    # Conversion can retain ICC/EXIF/text chunks in ``info``.
                    # Typst receives only neutral pixels, never source metadata.
                    normalized.info.clear()
                    normalized.save(destination, format="PNG", optimize=False)
                finally:
                    if normalized is not None:
                        normalized.close()
                    if corrected is not image:
                        corrected.close()
    except PipelineError:
        raise
    except Exception as exc:
        raise PipelineError(
            f"Markdown image at line {asset.first_line} could not be decoded safely"
        ) from exc
    finally:
        Image.MAX_IMAGE_PIXELS = previous_limit
    return decompressed_bytes


def _copy_pinned_input(
    source_stream: BinaryIO,
    destination: Path,
    *,
    expected_size: int,
) -> None:
    source_stream.seek(0)
    remaining = expected_size
    with destination.open("xb") as output_stream:
        while remaining:
            chunk = source_stream.read(min(64 * 1024, remaining))
            if not chunk:
                raise OSError("referenced image became shorter while it was copied")
            output_stream.write(chunk)
            remaining -= len(chunk)
        if source_stream.read(1):
            raise OSError("referenced image became longer while it was copied")


def _workspace_size(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def _check_temporary_limit(root: Path, limit: int | None) -> None:
    if limit is None:
        return
    used = _workspace_size(root)
    if used > limit:
        raise PipelineError(
            f"Markdown rendering used {used:,} temporary bytes, over the configured "
            f"limit of {limit:,} bytes"
        )


def _assert_regular_workspace_tree(root: Path) -> None:
    pending = [validate_path_before_access(root, what="Typst workspace")]
    while pending:
        directory = pending.pop()
        for child in directory.iterdir():
            checked = validate_path_before_access(child, what="Typst workspace entry")
            if checked.is_symlink():
                raise PipelineError("Typst workspace contains a symbolic-link or junction entry")
            if checked.is_dir():
                pending.append(checked)
            elif not checked.is_file():
                raise PipelineError("Typst workspace contains a non-regular entry")


def _same_existing_file(first: Path, second: Path) -> bool:
    try:
        return os.path.samefile(first, second)
    except OSError:
        return first.resolve(strict=False) == second.resolve(strict=False)


def _typst_manifest_path(value: str) -> Path:
    """Normalize Typst's Windows verbatim paths before containment checks."""
    if os.name == "nt" and value.startswith("\\\\?\\UNC\\"):
        return Path(f"\\\\{value[8:]}")
    if os.name == "nt" and value.startswith("\\\\?\\"):
        return Path(value[4:])
    return Path(value)


def _audit_typst_dependencies(
    dependency_manifest: Path,
    *,
    workspace: Path,
    expected_inputs: list[Path],
    expected_output: Path,
) -> None:
    try:
        if (
            not dependency_manifest.is_file()
            or dependency_manifest.stat().st_size > _MAX_DEPENDENCY_MANIFEST_BYTES
        ):
            raise ValueError
        payload = json.loads(dependency_manifest.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError
        raw_inputs = payload["inputs"]
        raw_outputs = payload["outputs"]
        if not isinstance(raw_inputs, list) or not all(
            isinstance(item, str) for item in raw_inputs
        ):
            raise ValueError
        if not isinstance(raw_outputs, list) or not all(
            isinstance(item, str) for item in raw_outputs
        ):
            raise ValueError
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, ValueError) as exc:
        raise PipelineError("Typst produced an invalid dependency manifest") from exc

    dependencies: list[Path] = []
    for raw_path in raw_inputs:
        try:
            dependency = ensure_contained(
                _typst_manifest_path(raw_path), workspace, what="Typst dependency"
            )
            dependency = validate_path_before_access(dependency, what="Typst dependency")
            if not dependency.is_file():
                raise FileNotFoundError(dependency)
        except (FileNotFoundError, OSError, PathSecurityError) as exc:
            raise PipelineError(
                "Typst reported a dependency outside its regular workspace"
            ) from exc
        dependencies.append(dependency)
    unmatched = list(expected_inputs)
    for dependency in dependencies:
        match = next(
            (
                expected
                for expected in unmatched
                if _same_existing_file(dependency, expected)
            ),
            None,
        )
        if match is None:
            raise PipelineError(
                "Typst read an unexpected dependency; generated output was refused"
            )
        unmatched.remove(match)
    if unmatched:
        raise PipelineError("Typst read an unexpected dependency; generated output was refused")

    outputs = [
        ensure_contained(_typst_manifest_path(raw), workspace, what="Typst output")
        for raw in raw_outputs
    ]
    if len(outputs) != 1 or not _same_existing_file(outputs[0], expected_output):
        raise PipelineError("Typst reported an unexpected output path")


def _remaining_timeout(context: JobContext) -> float:
    configured = context.limits.timeout_seconds
    if configured is None:
        return _MAX_TYPST_TIMEOUT_SECONDS
    elapsed = (datetime.now(UTC) - context.started_at).total_seconds()
    remaining = configured - elapsed
    if remaining <= 0:
        raise JobCancelled(f"Job {context.job_id} exceeded its {configured:g}s time limit")
    return remaining


def md_to_pdf(
    input_file: Path,
    output: Path,
    *,
    options: MdToPdfOptions | None = None,
    image_inputs: dict[str, Path] | None = None,
) -> ConversionReport:
    options = options or MdToPdfOptions()
    paper = normalize_paper(options.paper)
    validate_margin(options.margin_mm, paper)
    settings = options.settings or get_settings()
    registry = default_registry()
    engine = registry.engine_for(OP_MD_TO_PDF)
    engine_info = engine.probe()
    snapshot = _load_markdown_source(input_file, settings)
    markdown_path = snapshot.path
    original_source = snapshot.source
    dropped = _DropTracker()
    filtered_source = _drop_block_unsupported(original_source, dropped)
    tokens = _markdown_parser().parse(filtered_source)
    _enforce_token_limit(tokens)
    image_references = _image_references(tokens)
    _enforce_image_reference_limit(image_references)
    assets, workspace_by_target = _resolve_assets(
        markdown_path,
        image_references,
        settings=settings,
        image_inputs=image_inputs,
    )
    renderer = _TypstRenderer(
        workspace_by_image_target=workspace_by_target,
        dropped=dropped,
    )
    body = renderer.render(_build_tree(tokens))
    typst_source = _typst_document(
        body,
        paper=paper,
        margin_mm=options.margin_mm,
        toc=options.toc,
    )

    input_paths = [markdown_path, *(asset.source for asset in assets)]

    def execute(context: JobContext, artifacts: list[InputArtifact]) -> ExecuteResult:
        if not artifacts or artifacts[0].media_type != MARKDOWN_MEDIA_TYPE:
            raise PipelineError("Markdown input changed type while the job was starting")
        _verify_markdown_snapshot(snapshot, artifacts[0], settings=settings)
        if any(artifact.media_type not in _IMAGE_INPUT_TYPES for artifact in artifacts[1:]):
            raise PipelineError(
                "A referenced Markdown image changed type while the job was starting"
            )
        artifact_by_path = {artifact.path: artifact for artifact in artifacts[1:]}
        assets_dir = context.workspace / "assets"
        assets_dir.mkdir(mode=0o700)
        source_assets_dir = context.workspace / "source-assets"
        source_assets_dir.mkdir(mode=0o700)
        decompressed_bytes = 0
        normalized_assets: list[Path] = []
        for index, asset in enumerate(assets):
            context.emit("normalize-image", current=index, total=len(assets))
            artifact = artifact_by_path[asset.source]
            try:
                checked_source = validate_path_before_access(
                    asset.source,
                    what="referenced Markdown image",
                    require_local=settings.strict_offline,
                ).resolve(strict=True)
                if (
                    not _same_existing_file(checked_source, artifact.path)
                    or checked_source.stat().st_size != artifact.size_bytes
                ):
                    raise OSError("referenced image identity changed")
            except (FileNotFoundError, OSError, PathSecurityError) as exc:
                raise PipelineError(
                    "A referenced Markdown image changed after input validation"
                ) from exc
            source_snapshot = source_assets_dir / f"source-{index + 1:04d}.bin"
            try:
                with checked_source.open("rb") as source_stream:
                    opened_stat = os.fstat(source_stream.fileno())
                    rechecked_source = validate_path_before_access(
                        asset.source,
                        what="referenced Markdown image",
                        require_local=settings.strict_offline,
                    ).resolve(strict=True)
                    if (
                        not _same_existing_file(rechecked_source, artifact.path)
                        or not os.path.samestat(opened_stat, rechecked_source.stat())
                        or opened_stat.st_size != artifact.size_bytes
                    ):
                        raise OSError("referenced image identity changed while opening")
                    _copy_pinned_input(
                        source_stream,
                        source_snapshot,
                        expected_size=artifact.size_bytes,
                    )
                    copied_stat = os.fstat(source_stream.fileno())
                    if (
                        copied_stat.st_size != opened_stat.st_size
                        or copied_stat.st_mtime_ns != opened_stat.st_mtime_ns
                    ):
                        raise OSError("referenced image changed while it was copied")
                _check_temporary_limit(
                    context.workspace,
                    context.limits.max_temporary_bytes,
                )
                require_media_type(source_snapshot, artifact.media_type)
                destination = ensure_contained(
                    context.workspace / asset.workspace_name,
                    context.workspace,
                    what="normalized Markdown image",
                )
                with source_snapshot.open("rb") as source_stream:
                    decompressed_bytes += _normalize_asset(
                        asset,
                        destination,
                        source_stream=source_stream,
                        media_type=artifact.media_type,
                        max_pixels=context.limits.max_image_pixels,
                        remaining_decompressed_bytes=(
                            None
                            if context.limits.max_decompressed_bytes is None
                            else context.limits.max_decompressed_bytes - decompressed_bytes
                        ),
                        decompressed_limit=context.limits.max_decompressed_bytes,
                    )
            except PipelineError:
                raise
            except ContentTypeError as exc:
                raise PipelineError(
                    "A referenced Markdown image changed to unsupported content "
                    "after input validation"
                ) from exc
            except (FileNotFoundError, OSError, PathSecurityError) as exc:
                raise PipelineError(
                    "A referenced Markdown image changed while it was opened"
                ) from exc
            finally:
                source_snapshot.unlink(missing_ok=True)
            decompressed_limit = context.limits.max_decompressed_bytes
            if decompressed_limit is not None and decompressed_bytes > decompressed_limit:
                raise PipelineError(
                    "Decoded Markdown images exceed the configured "
                    f"{decompressed_limit:,}-byte limit"
                )
            normalized_assets.append(destination)

        source_path = context.workspace / "document.typ"
        candidate = context.workspace / "candidate.pdf"
        dependency_manifest = context.workspace / "typst-dependencies.json"
        packages_dir = context.workspace / "typst-packages"
        package_cache_dir = context.workspace / "typst-package-cache"
        engine_home = context.workspace / "typst-home"
        engine_temp = context.workspace / "typst-temp"
        for directory in (packages_dir, package_cache_dir, engine_home, engine_temp):
            directory.mkdir(mode=0o700)
        source_path.write_text(typst_source, encoding="utf-8", newline="\n")
        _check_temporary_limit(context.workspace, context.limits.max_temporary_bytes)
        _assert_regular_workspace_tree(context.workspace)
        context.emit("compile", message="rendering Markdown with Typst")
        try:
            result = run_tool(
                "typst",
                [
                    "compile",
                    "--format",
                    "pdf",
                    "--root",
                    str(context.workspace),
                    "--package-path",
                    str(packages_dir),
                    "--package-cache-path",
                    str(package_cache_dir),
                    "--creation-timestamp",
                    "0",
                    "--jobs",
                    "1",
                    "--deps",
                    str(dependency_manifest),
                    "--deps-format",
                    "json",
                    "--diagnostic-format",
                    "short",
                    str(source_path),
                    str(candidate),
                ],
                timeout=_remaining_timeout(context),
                cwd=context.workspace,
                max_output_bytes=_MAX_TOOL_OUTPUT_BYTES,
                env_extra={
                    "HOME": str(engine_home),
                    "TEMP": str(engine_temp),
                    "TMP": str(engine_temp),
                    "TMPDIR": str(engine_temp),
                },
            )
        except ToolTimeout as exc:
            raise JobCancelled(str(exc)) from exc
        except ToolError as exc:
            raise PipelineError("Typst could not be launched safely") from exc
        if result.returncode != 0:
            raise PipelineError(
                f"Typst failed to compile the generated document (exit {result.returncode}); "
                "diagnostics were withheld because they may contain document text"
            )
        context.check_cancelled()
        _assert_regular_workspace_tree(context.workspace)
        _check_temporary_limit(context.workspace, context.limits.max_temporary_bytes)
        if any(packages_dir.iterdir()) or any(package_cache_dir.iterdir()):
            raise PipelineError(
                "Typst attempted to use a package; network-capable packages are refused"
            )
        _audit_typst_dependencies(
            dependency_manifest,
            workspace=context.workspace,
            expected_inputs=[source_path, *normalized_assets],
            expected_output=candidate,
        )
        _check_temporary_limit(context.workspace, context.limits.max_temporary_bytes)
        try:
            page_count = count_pdf_pages(candidate)
        except Exception:
            # The standard pipeline validator owns malformed/missing candidate
            # reporting so callers receive a populated failed validation result.
            page_count = None
        page_limit = context.limits.max_pages
        if page_limit is not None and page_count is not None and page_count > page_limit:
            raise PipelineError(
                f"Generated PDF has {page_count} pages, over the configured limit of {page_limit}"
            )

        dropped = renderer.dropped
        warnings = [
            FidelityWarning(
                code=MARKDOWN_CONSTRUCT_DROPPED,
                message=(
                    f"Unsupported Markdown {item.construct} at line {item.line} was dropped."
                ),
                severity=WarningSeverity.WARNING,
            )
            for item in dropped
        ]
        warnings.append(
            FidelityWarning(
                code=SYSTEM_FONT_DEPENDENT,
                message=(
                    "Typst uses its embedded fonts first and may use installed system fonts for "
                    "glyph fallback; line breaks and page counts can vary across machines."
                ),
                severity=WarningSeverity.INFO,
            )
        )
        return ExecuteResult(
            candidates=[
                CandidateOutput(
                    workspace_path=candidate,
                    destination=output,
                    expected_pages=page_count,
                    render_all=True,
                )
            ],
            fidelity_warnings=warnings,
            output_page_count=page_count,
            details={
                "paper": paper[0],
                "margin_mm": options.margin_mm,
                "toc": options.toc,
                "source_lines": _source_line_count(original_source),
                "image_count": len(assets),
                "images_normalized_to_png": len(assets),
                "dropped_constructs": [
                    {"construct": item.construct, "line": item.line} for item in dropped
                ],
                "dropped_constructs_truncated": renderer.dropped_omitted > 0,
                "dropped_constructs_omitted": renderer.dropped_omitted,
                "dropped_construct_report_limit": _MAX_REPORTED_DROPPED_CONSTRUCTS,
                "font_policy": "typst-embedded-with-system-fallback",
                "typst_root": "private-job-workspace",
                "typst_packages": "disabled-and-audited-empty",
            },
        )

    return run_pipeline(
        operation=OP_MD_TO_PDF,
        input_paths=input_paths,
        execute=execute,
        engine_name=engine.name,
        engine_version=engine_info.version,
        input_types=(MARKDOWN_MEDIA_TYPE, *_IMAGE_INPUT_TYPES),
        collision=options.collision,
        settings=settings,
        progress=options.progress,
        max_text_input_bytes=snapshot.byte_limit,
    )
