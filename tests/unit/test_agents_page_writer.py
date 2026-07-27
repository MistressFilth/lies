from __future__ import annotations

from pydantic_ai.models.test import TestModel

from lies.agents.page_writer import PageDiff, page_writer_agent


def test_page_writer_agent_exists() -> None:
    agent = page_writer_agent(model=TestModel())
    assert agent is not None


def test_page_writer_returns_diffs() -> None:
    agent = page_writer_agent(model=TestModel())
    with agent.override(model=TestModel()):
        result = agent.run_sync("Create a page for entity 'postgres'.")
    assert result is not None
    diffs = result.output
    assert isinstance(diffs, list)
    # TestModel returns a default-constructed list of PageDiff.
    for diff in diffs:
        assert isinstance(diff, PageDiff)
