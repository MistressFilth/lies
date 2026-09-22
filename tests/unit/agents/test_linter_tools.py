"""Tests for the linter tool surface (N2).

Pin the three tool closures against a fixture wiki + memory service.
Pattern mirrors tests/unit/agents/test_librarian_subagent.py for the
librarian's tool registration.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lies.agents.linter import linter_agent
from lies.agents.linter_tools import register_linter_tools
from lies.memory.service import WikiMemoryService


@pytest.fixture
def wiki(tmp_path: Path):
    """Stub Wiki with a wiki_dir containing 3 sample pages."""
    wiki = MagicMock()
    wiki.wiki_dir = tmp_path / "wiki"
    wiki.wiki_dir.mkdir()
    (wiki.wiki_dir / "a.md").write_text("# A\nclaim one", encoding="utf-8")
    (wiki.wiki_dir / "b.md").write_text("# B\nclaim two", encoding="utf-8")
    (wiki.wiki_dir / "c.md").write_text("# C\nclaim three", encoding="utf-8")
    return wiki


@pytest.fixture
def memory_service() -> WikiMemoryService:
    return MagicMock(spec=WikiMemoryService)


@pytest.fixture
def agent():
    from pydantic_ai.models.test import TestModel

    return linter_agent(model=TestModel())


def test_register_linter_tools_attaches_three_tools(agent, wiki, memory_service) -> None:
    register_linter_tools(agent, wiki=wiki, memory_service=memory_service)
    tool_names = {tool.name for tool in agent.toolsets[0].tools.values()}
    assert {"wiki_list_pages", "wiki_search", "wiki_read"} <= tool_names


def test_wiki_list_pages_returns_inventory_json(agent, wiki, memory_service) -> None:
    register_linter_tools(agent, wiki=wiki, memory_service=memory_service)
    list_tool = next(t for t in agent.toolsets[0].tools.values() if t.name == "wiki_list_pages")
    payload = list_tool.function(None)
    rows = json.loads(payload)
    assert isinstance(rows, list)
    assert {row["path"] for row in rows} == {"a.md", "b.md", "c.md"}
    for row in rows:
        assert "size_estimate_tokens" in row
        assert isinstance(row["size_estimate_tokens"], int)
        assert row["size_estimate_tokens"] >= 1


def test_wiki_search_delegates_to_memory_service(agent, wiki, memory_service) -> None:
    from lies.memory.models import WikiSearchResult

    memory_service.search.return_value = WikiSearchResult(
        query="q",
        pages=[],
        truncated=False,
        fallback_used=False,
        fallback_reason="",
        no_coverage=False,
    )
    register_linter_tools(agent, wiki=wiki, memory_service=memory_service)
    search_tool = next(t for t in agent.toolsets[0].tools.values() if t.name == "wiki_search")
    payload = search_tool.function(None, "claim one", limit=3)
    memory_service.search.assert_called_once_with("claim one", limit=3)
    assert "pages" in payload


def test_wiki_read_returns_bodies_for_known_paths(agent, wiki, memory_service) -> None:
    memory_service.read.return_value = {
        "a.md": "# A\nclaim one",
        "b.md": "# B\nclaim two",
    }
    register_linter_tools(agent, wiki=wiki, memory_service=memory_service)
    read_tool = next(t for t in agent.toolsets[0].tools.values() if t.name == "wiki_read")
    payload = read_tool.function(None, ["a.md", "b.md"])
    assert payload == {"a.md": "# A\nclaim one", "b.md": "# B\nclaim two"}
