"""Tests for the F19 QueryDeps shape (librarian_output-driven)."""

from __future__ import annotations


def test_query_deps_accepts_librarian_output() -> None:
    """F19: QueryDeps gains librarian_output field."""
    from lies.agents.librarian import (
        LibrarianOutput,
        PageExcerpt,
    )
    from lies.agents.query_synthesizer import QueryDeps
    from lies.markdown_spans import Span

    excerpts = [
        PageExcerpt(
            collection="wiki",
            slug="x",
            title="X",
            spans=[Span(heading_path=["H1"], body="body", code_fence=False, start_line=1)],
        )
    ]
    lo = LibrarianOutput(tag_expr=None, exclude_tags=[], excerpts=excerpts, distinct_pages=1)
    deps = QueryDeps(question="q", librarian_output=lo)
    assert deps.librarian_output is lo


def test_query_deps_page_texts_derived_from_excerpts() -> None:
    """page_texts becomes a derived property concatenating non-code-fence spans."""
    from lies.agents.librarian import LibrarianOutput, PageExcerpt
    from lies.agents.query_synthesizer import QueryDeps
    from lies.markdown_spans import Span

    excerpts = [
        PageExcerpt(
            collection="wiki",
            slug="a",
            title="A",
            spans=[
                Span(heading_path=[], body="prose a", code_fence=False, start_line=1),
                Span(heading_path=[], body="x = 1", code_fence=True, start_line=3),
            ],
        ),
    ]
    lo = LibrarianOutput(tag_expr=None, exclude_tags=[], excerpts=excerpts, distinct_pages=1)
    deps = QueryDeps(question="q", librarian_output=lo)
    assert "prose a" in deps.page_texts["a"]
    assert "x = 1" not in deps.page_texts["a"]


def test_query_deps_page_sources_derived() -> None:
    from lies.agents.librarian import LibrarianOutput, PageExcerpt
    from lies.agents.query_synthesizer import QueryDeps
    from lies.markdown_spans import Span

    spans = [Span(heading_path=[], body="b", code_fence=False, start_line=1)]
    lo = LibrarianOutput(
        tag_expr=None,
        exclude_tags=[],
        excerpts=[
            PageExcerpt(collection="wiki", slug="w", title="W", spans=spans),
            PageExcerpt(collection="claude_platform", slug="c", title="C", spans=spans),
        ],
        distinct_pages=2,
    )
    deps = QueryDeps(question="q", librarian_output=lo)
    assert deps.page_sources["w"] == "wiki"
    assert deps.page_sources["c"] == "library"
