from __future__ import annotations

from datetime import date

from pydantic_ai.models.test import TestModel

from lies.agents.indexer import IndexerResult, format_log_entry, indexer_agent


def test_indexer_agent_exists() -> None:
    agent = indexer_agent(model=TestModel())
    assert agent is not None


def test_indexer_returns_result() -> None:
    agent = indexer_agent(model=TestModel())
    with agent.override(model=TestModel()):
        result = agent.run_sync("Update the index for a new entity page.")

    assert isinstance(result.output, IndexerResult)
    assert isinstance(result.output.index_content, str)
    assert isinstance(result.output.log_entry, str)


def test_format_log_entry_uses_parseable_prefix() -> None:
    entry = format_log_entry("ingest", "Postgres MVCC", date(2026, 7, 27))

    assert entry == "## [2026-07-27] ingest | Postgres MVCC"
