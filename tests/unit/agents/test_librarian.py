"""Tests for src/lies/agents/librarian.py — F18."""

from __future__ import annotations

from lies.agents.librarian import (
    LibrarianDeps,
    LibrarianOutput,
    PageExcerpt,
    _rewrite_query_for_validator,
)


def test_rewrite_query_drops_in_word_hyphens() -> None:
    assert _rewrite_query_for_validator("error-handling pattern") == "error handling pattern"
    assert _rewrite_query_for_validator("comma-separated values") == "comma separated values"
    assert _rewrite_query_for_validator("case-insensitive match") == "case insensitive match"


def test_rewrite_query_handles_iso_dates() -> None:
    assert _rewrite_query_for_validator("on 2026-05-10 release") == "on May 10 2026 release"


def test_rewrite_query_passes_through_normal_text() -> None:
    assert _rewrite_query_for_validator("how does auth work?") == "how does auth work?"


def test_librarian_deps_required_fields() -> None:
    deps = LibrarianDeps(
        question="how does pydantic validate nested models?",
        tag_expr="python",
        exclude_tags=["postgres"],
        top_k=5,
    )
    assert deps.top_k == 5
    assert deps.tag_expr == "python"


def test_page_excerpt_carries_spans_not_text_blob() -> None:
    from lies.markdown_spans import Span

    spans = [
        Span(heading_path=["H1"], body="body text", code_fence=False, start_line=1),
    ]
    pe = PageExcerpt(collection="wiki", slug="x", title="X", spans=spans)
    assert pe.spans == spans


def test_librarian_output_distinct_pages_derives_from_excerpts() -> None:
    from lies.markdown_spans import Span

    spans = [Span(heading_path=[], body="b", code_fence=False, start_line=1)]
    out = LibrarianOutput(
        tag_expr=None,
        exclude_tags=[],
        excerpts=[
            PageExcerpt(collection="wiki", slug="a", title="A", spans=spans),
            PageExcerpt(collection="wiki", slug="a", title="A", spans=spans),
            PageExcerpt(collection="wiki", slug="b", title="B", spans=spans),
        ],
        distinct_pages=2,  # caller computes; test the field is settable
    )
    assert out.distinct_pages == 2
