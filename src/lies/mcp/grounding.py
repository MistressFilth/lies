"""Grounding archivist — `ground()` MCP tool + Python function.

Wraps the F18 librarian (shipped at v0.33.0) to return a tight
grounding digest: up to ``top_k`` CitationSnippet entries (≤200
chars each), keyed by bare slug for ``[[slug]]: "snippet"``
rendering. Reuses the F37 span parser and F15 tag-filter dispatch.

Library vs wiki discrimination lives on ``CitationSnippet.collection``.
No LLM call in this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lies.markdown_spans import Span


@dataclass(frozen=True)
class CitationSnippet:
    """One grounding snippet — ≤200 chars from the first prose span of a page.

    Attributes:
        collection: ``"wiki"`` or library-collection-name (e.g.
            ``"claude_platform"``).
        slug: Bare slug for ``[[slug]]: "snippet"`` rendering. Examples:
            ``"concepts/pydantic"``, ``"entities/postgres"``.
        title: Human-readable page title.
        snippet: First ≤200 chars of the first prose span of the page.
    """

    collection: str
    slug: str
    title: str
    snippet: str


@dataclass(frozen=True)
class ArchivistDigest:
    """The grounding archivist's return value.

    Attributes:
        question: Echoed back for caller verification.
        tag_expr: Chosen union (``None`` when untagged).
        exclude_tags: NOT tags the caller passed through.
        citations: Snippets, one per retrieved excerpt.
        no_coverage: True when the corpus is non-empty AND no hits
            landed (scope miss on a populated wiki).
        distinct_pages: ``len({c.slug for c in citations})``.
    """

    question: str
    tag_expr: str | None
    exclude_tags: list[str]
    citations: list[CitationSnippet]
    no_coverage: bool
    distinct_pages: int


class ArchivistCoverageError(Exception):
    """Tagged query hit zero collections (unknown tag)."""


def truncate_at_word_boundary(text: str, max_chars: int) -> str:
    """Truncate ``text`` to ≤ ``max_chars``, cutting at the last whitespace.

    No trailing partial word; no trailing whitespace. If ``text``
    has no whitespace at all, hard-cut at ``max_chars``. ``max_chars``
    must be ≥ 1.

    Args:
        text: The string to truncate.
        max_chars: Maximum length of the result (must be ≥ 1).

    Returns:
        Truncated string of length ≤ ``max_chars``.

    Raises:
        ValueError: if ``max_chars < 1``.
    """
    if max_chars < 1:
        raise ValueError(f"max_chars must be ≥ 1, got {max_chars}")
    if len(text) <= max_chars:
        return text
    # Look for the last whitespace at or before position max_chars.
    candidate = text[:max_chars]
    if " " in candidate:
        # Strip everything from the last space onward.
        cut = candidate.rsplit(" ", 1)[0]
        return cut.rstrip()
    # No whitespace in the candidate — hard cut at max_chars.
    return candidate


def pick_first_prose_span(spans: "list[Span]") -> "Span | None":
    """Return the first prose span from ``spans``, or ``None``.

    Skips code-fence spans (``code_fence=True``) and empty bodies
    (``body.strip() == ""``). The synthesis's pipeline already
    separates prose from code, so this filter is the F19 ground
    shape's lens on F37 spans.
    """
    for span in spans:
        if span.code_fence:
            continue
        if not span.body.strip():
            continue
        return span
    return None
