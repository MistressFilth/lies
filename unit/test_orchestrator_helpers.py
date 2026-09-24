"""Tests for orchestrator-side claim validation + heading path threading (F19)."""

from __future__ import annotations

from lies.agents.librarian import LibrarianOutput, PageExcerpt
from lies.markdown_spans import Span
from lies.orchestrator import (
    _thread_heading_paths,
    _validate_claim_citations,
)
from lies.query.citation import Citation, ClaimCitation


def _excerpt(slug: str, spans: list[Span]) -> PageExcerpt:
    return PageExcerpt(collection="wiki", slug=slug, title=slug, spans=spans)


def test_validate_claim_citations_drops_missing_claim() -> None:
    lo = LibrarianOutput(
        tag_expr=None,
        exclude_tags=[],
        excerpts=[
            _excerpt("a", [Span(heading_path=[], body="body", code_fence=False, start_line=1)])
        ],
        distinct_pages=1,
    )
    citations = [Citation(path="a", source="wiki")]
    ccs = [ClaimCitation(claim="NOT IN BODY", citation_index=0, quote="body")]
    survivors = _validate_claim_citations("answer body", citations, ccs, lo)
    assert survivors == []


def test_validate_claim_citations_drops_bad_index() -> None:
    lo = LibrarianOutput(
        tag_expr=None,
        exclude_tags=[],
        excerpts=[
            _excerpt("a", [Span(heading_path=[], body="body", code_fence=False, start_line=1)])
        ],
        distinct_pages=1,
    )
    citations = [Citation(path="a", source="wiki")]
    ccs = [ClaimCitation(claim="body", citation_index=99, quote="body")]
    survivors = _validate_claim_citations("body", citations, ccs, lo)
    assert survivors == []


def test_validate_claim_citations_drops_quote_not_in_excerpt() -> None:
    lo = LibrarianOutput(
        tag_expr=None,
        exclude_tags=[],
        excerpts=[
            _excerpt(
                "a", [Span(heading_path=[], body="real body text", code_fence=False, start_line=1)]
            )
        ],
        distinct_pages=1,
    )
    citations = [Citation(path="a", source="wiki")]
    ccs = [ClaimCitation(claim="real body", citation_index=0, quote="fabricated quote")]
    survivors = _validate_claim_citations("real body", citations, ccs, lo)
    assert survivors == []


def test_validate_claim_citations_keeps_valid() -> None:
    lo = LibrarianOutput(
        tag_expr=None,
        exclude_tags=[],
        excerpts=[
            _excerpt(
                "a",
                [Span(heading_path=["H1"], body="real body text", code_fence=False, start_line=1)],
            )
        ],
        distinct_pages=1,
    )
    citations = [Citation(path="a", source="wiki")]
    ccs = [ClaimCitation(claim="real body text", citation_index=0, quote="real body text")]
    survivors = _validate_claim_citations("real body text", citations, ccs, lo)
    assert len(survivors) == 1


def test_thread_heading_paths_sets_citation_heading_path() -> None:
    span = Span(heading_path=["H1", "H2"], body="real body text", code_fence=False, start_line=1)
    lo = LibrarianOutput(
        tag_expr=None,
        exclude_tags=[],
        excerpts=[_excerpt("a", [span])],
        distinct_pages=1,
    )
    citations = [Citation(path="a", source="wiki")]
    ccs = [ClaimCitation(claim="real body text", citation_index=0, quote="real body text")]
    threaded = _thread_heading_paths(citations, ccs, lo)
    assert threaded[0].heading_path == ["H1", "H2"]


def test_thread_heading_paths_handles_empty_heading_path() -> None:
    span = Span(heading_path=[], body="body", code_fence=False, start_line=1)
    lo = LibrarianOutput(
        tag_expr=None,
        exclude_tags=[],
        excerpts=[_excerpt("a", [span])],
        distinct_pages=1,
    )
    citations = [Citation(path="a", source="wiki")]
    ccs = [ClaimCitation(claim="body", citation_index=0, quote="body")]
    threaded = _thread_heading_paths(citations, ccs, lo)
    assert threaded[0].heading_path == []
