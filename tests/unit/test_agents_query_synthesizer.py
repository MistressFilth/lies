from __future__ import annotations

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
    assert "--- " not in rendered
    assert rendered.strip() != ""
