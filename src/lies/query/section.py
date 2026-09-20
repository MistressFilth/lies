"""Section extraction from markdown bodies.

DEPRECATED (F19): use ``lies.markdown_spans.parse_spans`` for new
code. This module is retained for back-compat with non-F19 callers.
The implementation is unchanged; new code should consume
``PageRead.spans`` (a list of ``Span``) rather than calling
``_extract_section_at`` directly.
"""

from __future__ import annotations


def _extract_section_at(body: str, line: int) -> str | None:
    """Return the last ATX heading at or before ``line`` (1-indexed).

    .. deprecated::
        Use ``parse_spans(body)`` and read ``span.heading_path``
        instead. Retained for back-compat with non-F19 callers.
    """
    if line <= 0:
        return None
    last_heading: str | None = None
    for i, raw in enumerate(body.splitlines(), 1):
        stripped = raw.lstrip()
        if stripped.startswith("#") and not stripped.startswith("#!"):
            # 4+ space indent → not a heading per CommonMark.
            indent = len(raw) - len(stripped)
            if indent >= 4:
                continue
            heading_text = stripped.lstrip("#").strip()
            if heading_text:
                last_heading = heading_text
        if i == line:
            return last_heading
    return last_heading
