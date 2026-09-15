"""Format validators: verify the synthesizer's format_hint matches the body.

Pure functions; no I/O. Silent demotion to "md" on parse failure so the
operator never sees an error from validation alone.
"""

from __future__ import annotations

import re
from typing import Literal

_FENCE_RE = re.compile(
    r"^(```|~~~).*?^\1",
    re.MULTILINE | re.DOTALL,
)

# Header and data rows: optional leading whitespace, then a pipe-bounded
# line with at least one character between the pipes.
_TABLE_ROW_RE = re.compile(r"^\s*\|.+\|\s*$")

# Separator row: one or more pipe-separated cells, each cell being a
# `:?-+:?` alignment spec with optional surrounding whitespace. GFM
# permits up to 3 leading spaces; ``\s*`` at the head is permissive but
# never lets a separator accidentally match a data row (data rows have
# non-pipe content between their pipe-delimited cells).
_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|(?:\s*:?-+:?\s*\|)+\s*$")


def _strip_fenced(body: str) -> str:
    """Remove fenced code blocks (``` or ~~~) from ``body``.

    The fence regex matches balanced fences via backreference; unbalanced
    fences leave the body unchanged. Tables and Marp frontmatter markers
    inside code blocks are ignored by validators after this strip.
    """
    return _FENCE_RE.sub("", body)


def _validate_md(body: str) -> bool:
    """Markdown has no structural validator; always True."""
    return True


def _validate_table(body: str) -> bool:
    """Return True iff ``body`` contains at least one well-formed GFM table.

    A well-formed table: header row, separator row immediately after the
    header (blank lines may intervene, prose may not), and at least one
    data row. Fenced code blocks are excluded so pipes inside code don't
    count. The body may contain multiple tables; any valid one suffices.
    """
    stripped = _strip_fenced(body)
    lines = stripped.splitlines()

    i = 0
    while i < len(lines):
        if not _TABLE_ROW_RE.match(lines[i]):
            i += 1
            continue
        # Header matched; the very next non-blank line must be a separator.
        j = i + 1
        while j < len(lines) and lines[j].strip() == "":
            j += 1
        if j < len(lines) and _TABLE_SEPARATOR_RE.match(lines[j]):
            # Look for at least one data row after the separator; blanks
            # may intervene.
            k = j + 1
            while k < len(lines) and lines[k].strip() == "":
                k += 1
            if k < len(lines) and _TABLE_ROW_RE.match(lines[k]):
                return True
        i += 1
    return False


def _validate_marp(body: str) -> bool:
    """Return True iff ``body`` has `marp: true` frontmatter + ≥1 slide break.

    Frontmatter: the body starts with `---\\n...\\n---`. The frontmatter
    block must contain a `marp: true` (or `marp: yes`) directive.

    Slide break: a line that is exactly `---` AND is outside both the
    frontmatter block and any fenced code block. Must be ≥1.
    """
    lines = body.splitlines()
    if not lines or lines[0].strip() != "---":
        return False
    # Find the closing frontmatter `---`.
    fm_end = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            fm_end = idx
            break
    if fm_end is None:
        return False
    fm_block = "\n".join(lines[1:fm_end])
    if not re.search(r"^\s*marp:\s*(true|yes)\s*$", fm_block, re.MULTILINE):
        return False
    # Count slide breaks: `---` lines outside frontmatter and outside
    # fenced code blocks.
    body_after_fm = lines[fm_end + 1 :]
    in_fence = False
    fence_marker: str | None = None
    slide_breaks = 0
    for line in body_after_fm:
        stripped = line.strip()
        if in_fence:
            if fence_marker is not None and stripped == fence_marker:
                in_fence = False
                fence_marker = None
            continue
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = True
            fence_marker = stripped[:3]
            continue
        if stripped == "---":
            slide_breaks += 1
    return slide_breaks >= 1


def validate_format(
    body: str,
    hint: Literal["md", "table", "marp"],
) -> Literal["md", "table", "marp"]:
    """Validate ``hint`` against ``body``; silently demote to "md" on failure.

    The body is preserved unchanged; the returned hint is what callers
    should use downstream (the CLI render path, the MCP wire format,
    the file-back frontmatter).
    """
    if hint == "md":
        return "md"
    if hint == "table":
        return "table" if _validate_table(body) else "md"
    if hint == "marp":
        return "marp" if _validate_marp(body) else "md"
    return "md"


__all__ = ("validate_format",)
