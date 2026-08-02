from __future__ import annotations

from pydantic_ai.models.test import TestModel

from lies.agents.query_synthesizer import QueryAnswer, query_synthesizer_agent


def test_query_synthesizer_agent_exists() -> None:
    agent = query_synthesizer_agent(model=TestModel())
    assert agent is not None


def test_query_synthesizer_returns_answer() -> None:
    agent = query_synthesizer_agent(model=TestModel())
    with agent.override(model=TestModel()):
        result = agent.run_sync("What does my corpus say about X?")
    assert result is not None
    assert isinstance(result.output, QueryAnswer)
    assert isinstance(result.output.answer, str)
    assert isinstance(result.output.citations, list)
    assert isinstance(result.output.should_file, bool)
