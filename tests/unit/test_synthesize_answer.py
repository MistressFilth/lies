"""Tests for ``build_answer_from_pages`` preamble semantics.

The synthesizer renders one of three preambles based on ``fallback_reason``:

- ``""`` — no preamble (qmd served the query).
- ``FALLBACK_REASON_WIKI_ONLY`` — "not grounded in primary sources"
  preamble (library returned 0 hits, wiki surfaced content).
- Any other truthy reason — "qmd unavailable" preamble.

This module pins the WIKI_ONLY branch: the body must contain the
not-grounded phrase and must NOT contain the qmd-unavailable phrase.
The other two branches are covered by ``test_query_synthesizer.py``.
"""

from __future__ import annotations

from lies.query.synthesizer import (
    FALLBACK_REASON_WIKI_ONLY,
    PageRead,
    build_answer_from_pages,
)


def test_build_answer_from_pages_wiki_only_preamble() -> None:
    """WIKI_ONLY renders the not-grounded preamble, not the qmd-unavailable preamble."""
    page = PageRead(
        rel_path="concepts/x.md",
        title="X",
        excerpt="excerpt",
        source="wiki",
    )
    ans = build_answer_from_pages(
        question="test", pages=[page], fallback_reason=FALLBACK_REASON_WIKI_ONLY
    )
    assert "not grounded in primary sources" in ans.answer
    assert "qmd unavailable" not in ans.answer
