"""Markdown span parser — F37.

Splits a markdown body into named spans keyed by ATX heading path.
Used by the query synthesizer to attach heading context to each
citation, replacing the single-paragraph `excerpt` field on
``PageRead``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Span:
    """A contiguous range of a markdown body with heading context.

    Attributes:
        heading_path: Nested ATX heading text from H1 down to the
            span's parent. Empty when the span sits above any
            heading.
        body: Verbatim text of the span. For code-fence spans,
            contains the inner fenced content only (delimiters
            stripped — synthesizers and other downstream consumers
            inspect the fence contents, not the fence delimiters).
        code_fence: True when the span lives inside ``` or ~~~
            fences. Consumers (e.g. the synthesizer's ``page_texts``
            derivation) exclude these from prose excerpts.
        start_line: 1-indexed line number of the span's first line
            in the source body.
    """

    heading_path: list[str]
    body: str
    code_fence: bool
    start_line: int


def parse_spans(markdown_text: str) -> list[Span]:
    """Split ``markdown_text`` into spans keyed by heading path.

    Pure function. No I/O. Setext headings (``===`` / ``---``
    underlines) are out of scope for v1 — they're treated as body
    text. Indented ``#`` (4+ spaces) is not a heading per CommonMark.

    Code fences: a span opens at ``^[ \\t]*(`{3,}|~{3,})\\S*\\n``
    and closes at the matching fence delimiter. Fence contents
    form a single span with ``code_fence=True``.

    Returns an empty list when ``markdown_text`` is empty.
    """
    if not markdown_text:
        return []

    spans: list[Span] = []
    heading_stack: list[tuple[int, str]] = []  # (level, text)
    current_body_lines: list[str] = []
    current_start: int | None = None
    in_fence: bool = False
    fence_marker: str | None = None
    fence_start: int | None = None
    fence_body_lines: list[str] = []

    def _flush_body(end_line: int) -> None:
        """Close any open prose span at ``end_line``."""
        nonlocal current_body_lines, current_start
        if not current_body_lines and current_start is None:
            return
        # Skip whitespace-only bodies (e.g. leading blank lines before
        # the first heading). These would otherwise produce a span with
        # no content and a misleading start_line.
        if not any(line.strip() for line in current_body_lines):
            current_body_lines = []
            current_start = None
            return
        body = "\n".join(current_body_lines)
        spans.append(
            Span(
                heading_path=[t for _, t in heading_stack],
                body=body,
                code_fence=False,
                start_line=current_start or end_line,
            )
        )
        current_body_lines = []
        current_start = None

    def _flush_fence(end_line: int) -> None:
        """Close any open fence span at ``end_line``."""
        nonlocal in_fence, fence_marker, fence_start, fence_body_lines
        if not in_fence:
            return
        body = "\n".join(fence_body_lines)
        spans.append(
            Span(
                heading_path=[t for _, t in heading_stack],
                body=body,
                code_fence=True,
                start_line=fence_start or end_line,
            )
        )
        in_fence = False
        fence_marker = None
        fence_start = None
        fence_body_lines = []

    lines = markdown_text.splitlines()
    for i, raw in enumerate(lines, start=1):
        stripped = raw.lstrip()
        indent = len(raw) - len(stripped)

        if in_fence:
            # Closing fence: same marker character, length >= opening
            closing = stripped.rstrip()
            if (
                fence_marker is not None
                and len(closing) >= len(fence_marker)
                and all(c == fence_marker[0] for c in closing)
            ):
                _flush_fence(i)
                continue
            fence_body_lines.append(raw)
            continue

        # Detect fence open
        if stripped.startswith("```") or stripped.startswith("~~~"):
            marker = stripped[:3]
            _flush_body(i - 1)
            in_fence = True
            fence_marker = marker
            fence_start = i
            fence_body_lines = []
            continue

        # Detect ATX heading
        if stripped.startswith("#") and not stripped.startswith("#!") and indent < 4:
            level = 0
            for ch in stripped:
                if ch == "#":
                    level += 1
                else:
                    break
            heading_text = stripped[level:].strip()
            if heading_text and level <= 6:
                _flush_body(i - 1)
                # Pop deeper-or-equal headings, push new heading
                while heading_stack and heading_stack[-1][0] >= level:
                    heading_stack.pop()
                heading_stack.append((level, heading_text))
                current_start = i
                continue

        # Plain body line
        if current_start is None:
            current_start = i
        current_body_lines.append(raw)

    # End of input: flush any open span
    if in_fence:
        _flush_fence(len(lines))
    else:
        _flush_body(len(lines))

    return spans
