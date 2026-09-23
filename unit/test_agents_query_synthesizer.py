from __future__ import annotations

import pytest
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import RunContext

from lies.agents.librarian import LibrarianOutput, PageExcerpt
from lies.agents.query_synthesizer import (
    QueryAnswer,
    QueryDeps,
    _build_query_prompt,
    query_synthesizer_agent,
)
from lies.markdown_spans import Span
from lies.query.citation import ClaimCitation


def _deps() -> QueryDeps:
    """Build a QueryDeps carrying a wiki-sourced excerpt via LibrarianOutput.

    F19 (Task 5): the legacy ``page_texts`` / ``page_sources``
    constructor fields were dropped in favour of a single
    ``librarian_output`` field with derived properties. Tests that
    exercise the prompt-renderer contract (rendered corpus block,
    source tag inline) construct the deps through the F19 surface.
    """
    excerpt = PageExcerpt(
        collection="wiki",
        slug="wiki/concepts/alpha.md",
        title="Alpha",
        spans=[
            Span(heading_path=[], body="Alpha is the first letter.", code_fence=False, start_line=1)
        ],
    )
    return QueryDeps(
        question="What is alpha?",
        librarian_output=LibrarianOutput(
            tag_expr=None,
            exclude_tags=[],
            excerpts=[excerpt],
            distinct_pages=1,
        ),
    )


def test_query_synthesizer_agent_exists() -> None:
    agent = query_synthesizer_agent(model=TestModel())
    assert agent is not None


def test_query_synthesizer_returns_answer() -> None:
    agent = query_synthesizer_agent(model=TestModel())
    with agent.override(model=TestModel()):
        result = agent.run_sync("What does my corpus say about X?", deps=_deps())
    assert result is not None
    assert isinstance(result.output, QueryAnswer)
    assert isinstance(result.output.answer, str)
    assert isinstance(result.output.citations, list)
    assert isinstance(result.output.should_file, bool)


def test_build_query_prompt_includes_page_corpus() -> None:
    ctx = RunContext(deps=_deps(), model=TestModel(), usage=None, prompt="")
    rendered = _build_query_prompt(ctx)
    assert "--- [wiki] wiki/concepts/alpha.md ---" in rendered
    assert "Alpha is the first letter." in rendered


def test_build_query_prompt_includes_the_question() -> None:
    ctx = RunContext(deps=_deps(), model=TestModel(), usage=None, prompt="")
    assert "What is alpha?" in _build_query_prompt(ctx)


def test_build_query_prompt_survives_missing_deps() -> None:
    ctx = RunContext(deps=None, model=TestModel(), usage=None, prompt="")
    rendered = _build_query_prompt(ctx)
    # The static prompt must still render, and the corpus-rendering
    # function must NOT have appended a page (no question, no `--- wiki/...
    # ---` corpus block — the static prompt references that pattern as an
    # example but does not itself emit a page separator).
    assert "Question:" not in rendered
    assert rendered.strip() != ""


# ---------------------------------------------------------------------------
# Critical 4: QueryDeps carries a per-page source discriminator so the LLM
# can apply the library-wins-on-conflict rule from the prompt.
# ---------------------------------------------------------------------------


def test_query_deps_carries_page_sources() -> None:
    """``QueryDeps`` exposes ``page_sources`` keyed by the same path as
    ``page_texts`` so the prompt can render ``[library]``/``[wiki]``
    tags inline.

    F19 (Task 5): ``page_sources`` is now a derived property over
    ``LibrarianOutput.excerpts``. The discriminator carries through
    ``PageExcerpt.collection`` (``"library"`` / ``"wiki"`` / any
    other string → ``"library"``) so a librarian-built deps renders
    correctly. The legacy direct-field shape is gone.
    """
    excerpts = [
        PageExcerpt(
            collection="claude_platform",
            slug="claude_platform/skills.md",
            title="Skills",
            spans=[Span(heading_path=[], body="lib body", code_fence=False, start_line=1)],
        ),
        PageExcerpt(
            collection="wiki",
            slug="wiki/concepts/local.md",
            title="Local",
            spans=[Span(heading_path=[], body="wiki body", code_fence=False, start_line=1)],
        ),
    ]
    deps = QueryDeps(
        question="q",
        librarian_output=LibrarianOutput(
            tag_expr=None,
            exclude_tags=[],
            excerpts=excerpts,
            distinct_pages=2,
        ),
    )
    assert deps.page_sources == {
        "claude_platform/skills.md": "library",
        "wiki/concepts/local.md": "wiki",
    }


def test_query_deps_librarian_output_required() -> None:
    """``librarian_output`` is required (not defaulted) so a caller
    that forgets to populate it fails fast at construction rather
    than silently dropping the F19 evidence bundle from the prompt.

    Replaces the pre-F19 ``test_query_deps_page_sources_required``
    pin: the legacy fields are gone, the new required field is
    ``librarian_output``.
    """
    with pytest.raises(TypeError):
        QueryDeps(question="q")  # type: ignore[call-arg]


def test_build_query_prompt_renders_source_tag_inline() -> None:
    """The rendered corpus carries a ``[library]`` / ``[wiki]`` tag
    inline before each path so the LLM can apply the library-wins rule.

    F19 (Task 5): the corpus is derived from
    ``LibrarianOutput.excerpts`` (not direct ``page_texts`` /
    ``page_sources`` maps), but the rendered shape is unchanged.
    """
    excerpts = [
        PageExcerpt(
            collection="claude_platform",
            slug="claude_platform/skills.md",
            title="Skills",
            spans=[Span(heading_path=[], body="lib body", code_fence=False, start_line=1)],
        ),
        PageExcerpt(
            collection="wiki",
            slug="wiki/concepts/local.md",
            title="Local",
            spans=[Span(heading_path=[], body="wiki body", code_fence=False, start_line=1)],
        ),
    ]
    deps = QueryDeps(
        question="anything",
        librarian_output=LibrarianOutput(
            tag_expr=None,
            exclude_tags=[],
            excerpts=excerpts,
            distinct_pages=2,
        ),
    )
    ctx = RunContext(deps=deps, model=TestModel(), usage=None, prompt="")
    rendered = _build_query_prompt(ctx)
    # The library-sourced path renders with the [library] tag.
    assert "[library] claude_platform/skills.md" in rendered
    assert "lib body" in rendered
    # The wiki-sourced path renders with the [wiki] tag.
    assert "[wiki] wiki/concepts/local.md" in rendered
    assert "wiki body" in rendered


# ---------------------------------------------------------------------------
# Citation granularity: QueryAnswer carries claim_citations.
# ---------------------------------------------------------------------------


def test_query_answer_claim_citations_default_empty() -> None:
    qa = QueryAnswer(answer="x", citations=[], should_file=False)
    assert qa.claim_citations == []


def test_query_answer_accepts_claim_citations() -> None:
    cc = ClaimCitation(claim="c", citation_index=0)
    qa = QueryAnswer(
        answer="c",
        citations=["x.md"],
        should_file=False,
        claim_citations=[cc],
    )
    assert qa.claim_citations == [cc]
