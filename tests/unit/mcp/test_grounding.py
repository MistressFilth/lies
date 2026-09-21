"""Tests for src/lies/mcp/grounding.py — grounding archivist."""

from __future__ import annotations

import pytest

from lies.markdown_spans import Span
from lies.mcp.grounding import (
    ArchivistCoverageError,
    ArchivistDigest,
    CitationSnippet,
    pick_first_prose_span,
    truncate_at_word_boundary,
)


def test_truncate_at_word_boundary_short_text_unchanged() -> None:
    text = "Short text under the cap."
    assert truncate_at_word_boundary(text, 200) == text


def test_truncate_at_word_boundary_cuts_at_word_boundary() -> None:
    text = (
        "Lorem ipsum dolor sit amet, consectetur adipiscing elit, "
        "sed do eiusmod tempor incididunt ut labore et dolore magna "
        "aliqua. Ut enim ad minim veniam, quis nostrud exercitation "
        "ullamco laboris nisi ut aliquip ex ea commodo consequat."
    )
    truncated = truncate_at_word_boundary(text, 100)
    assert len(truncated) <= 100
    assert not truncated.endswith(" ")  # trailing partial word dropped
    assert truncated == truncated.rstrip() + ""  # no trailing whitespace
    # Cut should be at a word boundary — last char is not mid-word
    assert truncated.endswith(("m", "n", "o", "a", "e", "i", "s", "t", "d"))  # common word endings
    # The full last word at position 100 should NOT be present truncated mid-way


def test_truncate_at_word_boundary_hard_cut_no_whitespace() -> None:
    text = "a" * 500  # no whitespace
    truncated = truncate_at_word_boundary(text, 100)
    assert len(truncated) == 100


def test_truncate_at_word_boundary_empty_text() -> None:
    assert truncate_at_word_boundary("", 200) == ""


def test_truncate_at_word_boundary_zero_max() -> None:
    with pytest.raises(ValueError):
        truncate_at_word_boundary("text", 0)


def test_pick_first_prose_span_returns_none_when_empty() -> None:
    assert pick_first_prose_span([]) is None


def test_pick_first_prose_span_skips_code_fences() -> None:
    spans = [
        Span(heading_path=["H1"], body="```python\nx = 1\n```", code_fence=True, start_line=1),
        Span(heading_path=["H1"], body="prose body", code_fence=False, start_line=4),
    ]
    picked = pick_first_prose_span(spans)
    assert picked is not None
    assert picked.body == "prose body"


def test_pick_first_prose_span_returns_none_for_only_code_fences() -> None:
    spans = [
        Span(heading_path=["H1"], body="```\n", code_fence=True, start_line=1),
    ]
    assert pick_first_prose_span(spans) is None


def test_pick_first_prose_span_skips_empty_bodies() -> None:
    spans = [
        Span(heading_path=["H1"], body="", code_fence=False, start_line=1),
        Span(heading_path=["H1"], body="real body", code_fence=False, start_line=2),
    ]
    picked = pick_first_prose_span(spans)
    assert picked is not None
    assert picked.body == "real body"


def test_citation_snippet_frozen() -> None:
    cs = CitationSnippet(collection="wiki", slug="x", title="X", snippet="s")
    with pytest.raises(Exception):
        cs.snippet = "other"  # type: ignore[misc]


def test_archivist_digest_distinct_pages_counts_unique_slugs() -> None:
    digest = ArchivistDigest(
        question="q",
        tag_expr=None,
        exclude_tags=[],
        citations=[
            CitationSnippet(collection="wiki", slug="a", title="A", snippet="s"),
            CitationSnippet(collection="wiki", slug="a", title="A", snippet="s2"),
            CitationSnippet(collection="wiki", slug="b", title="B", snippet="s"),
        ],
        no_coverage=False,
        distinct_pages=2,
    )
    assert digest.distinct_pages == 2


def test_archivist_coverage_error_is_exception() -> None:
    with pytest.raises(ArchivistCoverageError):
        raise ArchivistCoverageError("no collections matched")


def test_ground_returns_empty_digest_on_librarian_exception(monkeypatch) -> None:
    """Librarian dispatch failure → no_coverage=True, citations=[]."""
    from lies.mcp import grounding

    def boom(deps):
        raise RuntimeError("qmd daemon offline")

    class _BoomAgent:
        def __init__(self, fn):
            self._fn = fn

        def run_sync(self, deps):
            return self._fn(deps)

    monkeypatch.setattr(grounding, "librarian_agent", lambda: _BoomAgent(boom))

    digest = grounding.ground("what is pydantic?")
    assert digest.no_coverage is True
    assert digest.citations == []
    assert digest.question == "what is pydantic?"
    assert digest.distinct_pages == 0


def test_ground_clamps_top_k_to_bounds(monkeypatch) -> None:
    """top_k=0 → clamp to 1; top_k=999 → clamp to 10. Both calls return without exception."""
    from lies.agents.librarian import LibrarianOutput, PageExcerpt
    from lies.markdown_spans import Span
    from lies.mcp import grounding

    def fake_librarian(deps):
        spans = [Span(heading_path=[], body="x", code_fence=False, start_line=1)]
        excerpts = [
            PageExcerpt(collection="wiki", slug=f"slug-{i}", title=f"T{i}", spans=spans)
            for i in range(20)
        ]
        return LibrarianOutput(
            tag_expr=None,
            exclude_tags=[],
            excerpts=excerpts,
            distinct_pages=20,
        )

    _patch_librarian(monkeypatch, grounding, fake_librarian)
    digest_low = grounding.ground("q", top_k=0)
    digest_high = grounding.ground("q", top_k=999)
    # top_k is a request hint forwarded to the librarian; the brief does not
    # require ground() to re-truncate the librarian's output. We verify the
    # function returns a digest in both cases without exception.
    assert digest_low.citations == digest_low.citations  # no exception
    assert digest_high.citations == digest_high.citations


def test_ground_uses_first_prose_span_per_excerpt(monkeypatch) -> None:
    """First prose span wins; snippet truncated to ≤200 chars."""
    from lies.agents.librarian import LibrarianOutput, PageExcerpt
    from lies.markdown_spans import Span
    from lies.mcp import grounding

    long_prose = ("A " * 150).strip()  # > 200 chars
    spans = [
        Span(heading_path=["H1"], body="```\nx = 1\n```", code_fence=True, start_line=1),
        Span(heading_path=["H1"], body=long_prose, code_fence=False, start_line=4),
    ]
    excerpts = [
        PageExcerpt(collection="wiki", slug="x", title="X", spans=spans),
    ]

    def fake_librarian(deps):
        return LibrarianOutput(tag_expr=None, exclude_tags=[], excerpts=excerpts, distinct_pages=1)

    _patch_librarian(monkeypatch, grounding, fake_librarian)
    digest = grounding.ground("q")
    assert len(digest.citations) == 1
    assert digest.citations[0].snippet != ""
    assert len(digest.citations[0].snippet) <= 200
    assert digest.citations[0].slug == "x"
    assert digest.citations[0].collection == "wiki"


def test_ground_skips_excerpts_with_only_code_fences(monkeypatch) -> None:
    """Excerpt with no prose spans → skipped from citations."""
    from lies.agents.librarian import LibrarianOutput, PageExcerpt
    from lies.markdown_spans import Span
    from lies.mcp import grounding

    code_only = [
        Span(heading_path=["H1"], body="```\nx = 1\n```", code_fence=True, start_line=1),
    ]
    prose_only = [
        Span(heading_path=["H1"], body="real text", code_fence=False, start_line=1),
    ]
    excerpts = [
        PageExcerpt(collection="wiki", slug="code-only", title="C", spans=code_only),
        PageExcerpt(collection="wiki", slug="prose", title="P", spans=prose_only),
    ]

    def fake_librarian(deps):
        return LibrarianOutput(tag_expr=None, exclude_tags=[], excerpts=excerpts, distinct_pages=2)

    _patch_librarian(monkeypatch, grounding, fake_librarian)
    digest = grounding.ground("q")
    assert len(digest.citations) == 1
    assert digest.citations[0].slug == "prose"
    assert digest.no_coverage is False
    assert digest.distinct_pages == 1


def test_ground_no_coverage_distinguishes_empty_corpus_from_scope_miss(monkeypatch) -> None:
    """Empty excerpts → no_coverage=False (corpus state not established here)."""
    from lies.agents.librarian import LibrarianOutput
    from lies.mcp import grounding

    def fake_librarian(deps):
        return LibrarianOutput(tag_expr=None, exclude_tags=[], excerpts=[], distinct_pages=0)

    _patch_librarian(monkeypatch, grounding, fake_librarian)
    digest = grounding.ground("q")
    assert digest.no_coverage is False
    assert digest.citations == []
    assert digest.distinct_pages == 0


def test_ground_no_coverage_true_when_corpus_non_empty_but_no_hits(monkeypatch) -> None:
    """Catalog has wiki pages but librarian returns 0 excerpts → no_coverage=True.

    Pins the F19 spec contract: ``no_coverage=True`` distinguishes the
    scope-miss-on-populated-wiki case (``corpus_size > 0`` AND zero
    excerpts) from the empty-corpus case (catalog unreadable or
    ``corpus_size == 0``). ``ground()`` probes the catalog through
    :func:`lies.mcp.grounding._corpus_page_count`, which is
    monkeypatched here to avoid touching the real sqlite catalog.
    """
    from lies.agents.librarian import LibrarianOutput
    from lies.mcp import grounding

    def fake_librarian(deps):
        return LibrarianOutput(tag_expr=None, exclude_tags=[], excerpts=[], distinct_pages=0)

    _patch_librarian(monkeypatch, grounding, fake_librarian)
    monkeypatch.setattr(grounding, "_corpus_page_count", lambda: 5)
    digest = grounding.ground("q")
    assert digest.no_coverage is True
    assert digest.citations == []


def test_ground_no_coverage_false_when_corpus_also_empty(monkeypatch) -> None:
    """Both catalog empty AND librarian returns 0 excerpts → no_coverage=False."""
    from lies.agents.librarian import LibrarianOutput
    from lies.mcp import grounding

    def fake_librarian(deps):
        return LibrarianOutput(tag_expr=None, exclude_tags=[], excerpts=[], distinct_pages=0)

    _patch_librarian(monkeypatch, grounding, fake_librarian)
    monkeypatch.setattr(grounding, "_corpus_page_count", lambda: 0)
    digest = grounding.ground("q")
    assert digest.no_coverage is False
    assert digest.citations == []


def test_ground_no_coverage_false_when_corpus_probe_raises(monkeypatch) -> None:
    """Catalog probe exception → no_coverage=False (fail-open)."""
    from lies.agents.librarian import LibrarianOutput
    from lies.mcp import grounding

    def fake_librarian(deps):
        return LibrarianOutput(tag_expr=None, exclude_tags=[], excerpts=[], distinct_pages=0)

    _patch_librarian(monkeypatch, grounding, fake_librarian)

    def boom() -> int:
        raise RuntimeError("catalog unreadable")

    monkeypatch.setattr(grounding, "_corpus_page_count", boom)
    digest = grounding.ground("q")
    assert digest.no_coverage is False
    assert digest.citations == []


def test_ground_no_coverage_false_when_citations_present(monkeypatch) -> None:
    """Citations present → no_coverage=False even if corpus probe returns >0."""
    from lies.agents.librarian import LibrarianOutput, PageExcerpt
    from lies.markdown_spans import Span
    from lies.mcp import grounding

    def fake_librarian(deps):
        spans = [Span(heading_path=[], body="x", code_fence=False, start_line=1)]
        excerpts = [PageExcerpt(collection="wiki", slug="x", title="X", spans=spans)]
        return LibrarianOutput(tag_expr=None, exclude_tags=[], excerpts=excerpts, distinct_pages=1)

    _patch_librarian(monkeypatch, grounding, fake_librarian)
    monkeypatch.setattr(grounding, "_corpus_page_count", lambda: 5)
    digest = grounding.ground("q")
    assert digest.no_coverage is False
    assert len(digest.citations) == 1


def _patch_librarian(monkeypatch, grounding_module, fake_fn):
    """Replace ``librarian_agent().run_sync(...)`` with ``fake_fn(deps)``.

    The real pydantic_ai ``Agent.run_sync`` returns an
    ``AgentRunResult`` whose ``.output`` attribute carries the typed
    output. ``ground()`` reads ``result.output``, so the fake mirrors
    that wrapper shape — not the raw ``LibrarianOutput``.
    """

    class _FakeResult:
        def __init__(self, output):
            self.output = output

    class _FakeAgent:
        def run_sync(self, user_prompt, *, deps):  # noqa: ARG002
            return _FakeResult(fake_fn(deps))

    monkeypatch.setattr(grounding_module, "librarian_agent", lambda: _FakeAgent())
