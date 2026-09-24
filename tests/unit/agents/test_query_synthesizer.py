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
    lo = LibrarianOutput(tag_expr=None, exclude_expr=None, excerpts=excerpts, distinct_pages=1)
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
    lo = LibrarianOutput(tag_expr=None, exclude_expr=None, excerpts=excerpts, distinct_pages=1)
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
        exclude_expr=None,
        excerpts=[
            PageExcerpt(collection="wiki", slug="w", title="W", spans=spans),
            PageExcerpt(collection="claude_platform", slug="c", title="C", spans=spans),
        ],
        distinct_pages=2,
    )
    deps = QueryDeps(question="q", librarian_output=lo)
    assert deps.page_sources["w"] == "wiki"
    assert deps.page_sources["c"] == "library"


# --- F1 chart format addendum (chart-variant prompt selector) ---

from lies.agents.query_synthesizer import (  # noqa: E402
    QUERY_SYNTHESIZER_CHART_PROMPT,
    _system_prompt_for_format,
)


def test_system_prompt_for_format_default_returns_standard() -> None:
    """No format hint → standard synthesizer prompt."""
    prompt = _system_prompt_for_format(None)
    assert "format_hint" in prompt
    assert "mermaid" not in prompt.lower().split("format reference")[0]


def test_system_prompt_for_format_md_returns_standard() -> None:
    """Auto-route to md → standard synthesizer prompt (chart not requested)."""
    prompt = _system_prompt_for_format("md")
    assert prompt == _system_prompt_for_format(None)


def test_system_prompt_for_format_chart_returns_chart_variant() -> None:
    """format_hint='chart' → chart-variant prompt with mermaid rules."""
    prompt = _system_prompt_for_format("chart")
    assert prompt == QUERY_SYNTHESIZER_CHART_PROMPT
    assert "flowchart" in prompt
    assert "sequenceDiagram" in prompt
    assert "classDiagram" in prompt


def test_chart_prompt_includes_one_fence_contract() -> None:
    """Chart variant must instruct: one fence, no other prose."""
    prompt = QUERY_SYNTHESIZER_CHART_PROMPT
    assert "```mermaid" in prompt
    # The contract says one fence per answer.
    assert "one" in prompt.lower() or "single" in prompt.lower()
