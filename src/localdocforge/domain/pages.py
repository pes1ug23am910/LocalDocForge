"""Page-range grammar shared by the CLI, API, and pipelines.

Grammar — comma-separated tokens over 1-based page numbers:

    1           single page
    1-5         ascending range (inclusive)
    5-1         descending range (explicit reorder)
    12-end      from page 12 through the final page
    end         the final page (alias of ``last``)
    odd         every odd-numbered page
    even        every even-numbered page
    all         every page in document order
    reverse     every page in reverse order
    last        the final page
    last-5      the final 5 pages, in document order

Notes:

- ``last-N`` means "the last N pages" (English reading), not a range from
  ``last`` down to page ``N``. A descending span is written numerically,
  e.g. ``9-3``.
- Tokens may repeat; a repeated page is included every time it appears
  (useful for duplication).
- Syntax errors raise at parse time; bounds are checked against the real
  page count in :meth:`PageRange.resolve` before any processing starts.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from pydantic import BaseModel, field_validator

_NUM = re.compile(r"^\d+$")
_NUM_SPAN = re.compile(r"^(\d+)-(\d+|end)$")
_LAST_N = re.compile(r"^last-(\d+)$")
_KEYWORDS = frozenset({"odd", "even", "all", "reverse", "last", "end"})


class PageRangeError(ValueError):
    """Raised for a syntactically or semantically invalid page range."""


def _parse_tokens(spec: str) -> list[str]:
    tokens = [token.strip().lower() for token in spec.split(",")]
    if not tokens or any(not token for token in tokens):
        raise PageRangeError(f"Empty token in page range: {spec!r}")
    for token in tokens:
        if token in _KEYWORDS or _NUM.match(token) or _LAST_N.match(token):
            continue
        span = _NUM_SPAN.match(token)
        if span:
            if span.group(1) == "0":
                raise PageRangeError(f"Pages are numbered from 1, got {token!r}")
            continue
        raise PageRangeError(
            f"Invalid page-range token {token!r}. Expected forms: 7, 2-9, 9-2, "
            f"12-end, odd, even, all, reverse, last, last-5"
        )
    for token in tokens:
        if _NUM.match(token) and int(token) == 0:
            raise PageRangeError("Pages are numbered from 1, got 0")
        last_n = _LAST_N.match(token)
        if last_n and int(last_n.group(1)) == 0:
            raise PageRangeError("last-N requires N >= 1, got last-0")
    return tokens


def _resolve_token(token: str, total: int) -> Iterator[int]:
    if token == "all":
        yield from range(1, total + 1)
        return
    if token == "reverse":
        yield from range(total, 0, -1)
        return
    if token == "odd":
        yield from range(1, total + 1, 2)
        return
    if token == "even":
        yield from range(2, total + 1, 2)
        return
    if token in {"last", "end"}:
        yield total
        return
    last_n = _LAST_N.match(token)
    if last_n:
        count = int(last_n.group(1))
        if count > total:
            raise PageRangeError(
                f"'last-{count}' requests {count} pages but the document has {total}"
            )
        yield from range(total - count + 1, total + 1)
        return
    span = _NUM_SPAN.match(token)
    if span:
        start = int(span.group(1))
        stop = total if span.group(2) == "end" else int(span.group(2))
        for bound in (start, stop):
            if bound > total:
                raise PageRangeError(f"Page {bound} is out of bounds (document has {total} pages)")
        step = 1 if stop >= start else -1
        yield from range(start, stop + step, step)
        return
    page = int(token)
    if page > total:
        raise PageRangeError(f"Page {page} is out of bounds (document has {total} pages)")
    yield page


class PageRange(BaseModel):
    """A validated page-range expression, resolved lazily against a page count."""

    model_config = {"frozen": True}

    spec: str = "all"

    @field_validator("spec")
    @classmethod
    def _validate_spec(cls, value: str) -> str:
        _parse_tokens(value)
        return value

    def resolve(self, total_pages: int) -> tuple[int, ...]:
        """Return the 1-based page sequence for a document with ``total_pages``."""
        if total_pages < 1:
            raise PageRangeError("Document has no pages")
        pages: list[int] = []
        for token in _parse_tokens(self.spec):
            pages.extend(_resolve_token(token, total_pages))
        if not pages:
            raise PageRangeError(f"Page range {self.spec!r} selects no pages")
        return tuple(pages)

    def __str__(self) -> str:  # pragma: no cover - display convenience
        return self.spec
