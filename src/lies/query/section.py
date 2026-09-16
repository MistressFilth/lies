"""Section extraction from markdown bodies.

Pure-function helpers used by the query synthesizer to attach a
human-readable section name to each citation, derived from the line
number qmd returns with each hit.
"""

from __future__ import annotations


def _extract_section_at(body: str, line: int) -> str | None:
    """Return the last ATX heading at or before ``line`` (1-indexed).

    Scans ``body`` line by line. An ATX heading is any line whose
    first non-whitespace character is ``#``, ``##``, ``###``, etc.,
    followed by space and heading text. Indented ``#`` (4+ spaces)
    is not a heading. Setext headings (``===`` / ``---`` underlines)
    are out of scope for v1.

    Returns ``None`` if no ATX heading precedes ``line``. When ``line``
    is past EOF, returns the last heading seen (or ``None`` if none).
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
