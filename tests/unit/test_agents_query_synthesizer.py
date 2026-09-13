from __future__ import annotations

import pytest
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import RunContext

from lies.agents.query_synthesizer import (
    QueryAnswer,
    QueryDeps,
    _build_query_prompt,
    query_synthesizer_agent,
)


def _deps() -> QueryDeps:
    return QueryDeps(
        question="What is alpha?",
        page_texts={"wiki/concepts/alpha.md": "Alpha is the first letter."},
        page_sources={"wiki/concepts/alpha.md": "wiki"},
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
    assert "--- wiki/concepts/alpha.md ---" in rendered
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
    tags inline."""
    deps = QueryDeps(
        question="q",
        page_texts={
            "claude_platform/skills.md": "lib body",
            "wiki/concepts/local.md": "wiki body",
        },
        page_sources={
            "claude_platform/skills.md": "library",
            "wiki/concepts/local.md": "wiki",
        },
    )
    assert deps.page_sources == {
        "claude_platform/skills.md": "library",
        "wiki/concepts/local.md": "wiki",
    }


def test_query_deps_page_sources_required() -> None:
    """``page_sources`` is required (not defaulted) so a caller that
    forgets to populate it fails fast at construction rather than
    silently dropping source info from the prompt."""
    with pytest.raises(TypeError):
        QueryDeps(question="q", page_texts={})  # type: ignore[call-arg]


def test_build_query_prompt_renders_source_tag_inline() -> None:
    """The rendered corpus carries a ``[library]`` / ``[wiki]`` tag
    inline before each path so the LLM can apply the library-wins rule."""
    deps = QueryDeps(
        question="anything",
        page_texts={
            "claude_platform/skills.md": "lib body",
            "wiki/concepts/local.md": "wiki body",
        },
        page_sources={
            "claude_platform/skills.md": "library",
            "wiki/concepts/local.md": "wiki",
        },
    )
    ctx = RunContext(deps=deps, model=TestModel(), usage=None, prompt="")
    rendered = _build_query_prompt(ctx)
    # The library-sourced path renders with the [library] tag.
    assert "[library] claude_platform/skills.md" in rendered
    assert "lib body" in rendered
    # The wiki-sourced path renders with the [wiki] tag.
    assert "[wiki] wiki/concepts/local.md" in rendered
    assert "wiki body" in rendered
