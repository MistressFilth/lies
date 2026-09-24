from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic_ai.models.test import TestModel

from lies.agents import read_file
from lies.agents.source_reader import SourceExtraction, source_reader_agent


@pytest.fixture
def markdown_source(tmp_path: Path) -> Path:
    src = tmp_path / "raw" / "article.md"
    src.parent.mkdir(parents=True)
    src.write_text(
        "# Postgres MVCC\n\n"
        "PostgreSQL uses Multi-Version Concurrency Control (MVCC) to allow readers "
        "and writers to operate without blocking each other. Each row has xmin and "
        "xmax system columns that track the inserting and deleting transactions.\n"
    )
    return src


def test_source_reader_agent_exists() -> None:
    agent = source_reader_agent(model=TestModel())
    assert agent is not None


def test_source_reader_registers_read_file_tool() -> None:
    """The agent should expose the `read_file` tool the system prompt advertises."""
    agent = source_reader_agent(model=TestModel())
    assert "read_file" in agent._function_toolset.tools


def test_read_file_tool_returns_content(markdown_source: Path) -> None:
    """The `read_file` tool returns the file's UTF-8 contents."""
    # ctx is unused by the tool; pass None to bypass RunContext construction.
    content = asyncio.run(
        read_file(None, str(markdown_source), str(markdown_source.parents[1] / "raw"))
    )  # type: ignore[arg-type]
    assert content.startswith("# Postgres MVCC")
    assert "MVCC" in content


def test_read_file_tool_reports_missing_file(tmp_path: Path) -> None:
    """The `read_file` tool returns an explicit error for a missing file."""
    missing = tmp_path / "does_not_exist.md"
    content = asyncio.run(read_file(None, str(missing), str(tmp_path / "raw")))  # type: ignore[arg-type]
    assert content.startswith("ERROR")
    assert str(missing) in content


def test_source_reader_returns_extraction(markdown_source: Path) -> None:
    """With TestModel, the agent returns a default-constructed SourceExtraction."""
    agent = source_reader_agent(model=TestModel())
    with agent.override(model=TestModel()):
        result = agent.run_sync(f"Read this source: {markdown_source}")

    extraction = result.output
    assert isinstance(extraction, SourceExtraction)
    assert isinstance(extraction.claims, list)
    assert isinstance(extraction.entities, list)
    assert isinstance(extraction.concepts, list)
    assert isinstance(extraction.comparisons, list)
    assert isinstance(extraction.summary, str)
