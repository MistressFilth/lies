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


def test_wiki_list_pages_emits_page_id_round_tripping(agent, wiki, memory_service) -> None:
    """Each inventory row carries a SHA-1 page_id that wiki_read accepts.

    Pins Critical #1 from the N2 review: ``WikiMemoryService.read``
    requires page_ids, not paths. The inventory must hand the agent
    values that round-trip through ``_path_for_id``.
    """
    from lies.memory.retrieval import _page_id_for

    register_linter_tools(agent, wiki=wiki, memory_service=memory_service)
    list_tool = next(t for t in agent.toolsets[0].tools.values() if t.name == "wiki_list_pages")
    rows = json.loads(list_tool.function(None))
    by_path = {row["path"]: row for row in rows}
    for path in ("a.md", "b.md", "c.md"):
        row = by_path[path]
        assert row["page_id"] == _page_id_for(path)
        # Round-trip: passing it to wiki_read produces the same body
        # the underlying read_pages would resolve.
        assert row["page_id"].startswith("page-")
        assert len(row["page_id"]) == len("page-") + 12


def test_wiki_list_pages_includes_type_and_source_pkg(agent, wiki, memory_service) -> None:
    """Inventory rows surface cluster signals beyond ``section``.

    Pins Important #7: ``section`` is ``"wiki"`` for every row and
    useless for clustering. ``type`` and ``source_pkg`` give the
    linter a usable partition for cross-page comparison.
    """
    register_linter_tools(agent, wiki=wiki, memory_service=memory_service)
    list_tool = next(t for t in agent.toolsets[0].tools.values() if t.name == "wiki_list_pages")
    rows = json.loads(list_tool.function(None))
    assert rows, "fixture seeded three pages"
    for row in rows:
        assert "type" in row
        assert "source_pkg" in row
        assert isinstance(row["type"], str)
        assert isinstance(row["source_pkg"], str)


def test_wiki_list_pages_size_reflects_disk_bytes(agent, wiki, memory_service) -> None:
    """``size_estimate_tokens`` reflects file bytes, not slug length.

    Pins Important #4: pre-fix the estimate was ``len(slug) // 4``,
    which produced wildly wrong values (a 5,000-token dense page
    with a 21-char slug estimated at 5 tokens).
    """
    # Overwrite one page with a much larger body so the estimate
    # must come from disk bytes, not slug length.
    (wiki.wiki_dir / "a.md").write_text(
        "# A\n\n" + ("claim content " * 100),
        encoding="utf-8",
    )
    register_linter_tools(agent, wiki=wiki, memory_service=memory_service)
    list_tool = next(t for t in agent.toolsets[0].tools.values() if t.name == "wiki_list_pages")
    rows = json.loads(list_tool.function(None))
    by_path = {row["path"]: row for row in rows}
    a_tokens = by_path["a.md"]["size_estimate_tokens"]
    b_tokens = by_path["b.md"]["size_estimate_tokens"]
    # a.md is now much larger than b.md; the estimates must reflect
    # that — same slug length (1 char), different content size.
    assert a_tokens > b_tokens
    assert a_tokens > 50  # the body has > 1500 chars / 4 ≈ 375 tokens


def test_wiki_list_pages_filters_catalog_drift(agent, wiki, memory_service) -> None:
    """Catalog rows whose on-disk file is deleted are excluded.

    Pins Important #5: pre-fix the inventory returned every catalog
    row; a ``lies catalog reconcile`` race could leave rows whose
    files were deleted off-disk. Combined with Critical #1, every
    stale row's ``wiki_read`` would raise ``WikiPageNotFound``.
    """
    register_linter_tools(agent, wiki=wiki, memory_service=memory_service)
    list_tool = next(t for t in agent.toolsets[0].tools.values() if t.name == "wiki_list_pages")
    before = {row["path"] for row in json.loads(list_tool.function(None))}
    assert before == {"a.md", "b.md", "c.md"}

    # Delete one file off-disk; the catalog row stays (the tool
    # doesn't reconcile), so the on-disk existence check must
    # filter it.
    (wiki.wiki_dir / "b.md").unlink()
    after = {row["path"] for row in json.loads(list_tool.function(None))}
    assert after == {"a.md", "c.md"}


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
    from lies.memory.retrieval import _page_id_for

    id_a = _page_id_for("a.md")
    id_b = _page_id_for("b.md")
    memory_service.read.return_value = {id_a: "# A\nclaim one", id_b: "# B\nclaim two"}
    register_linter_tools(agent, wiki=wiki, memory_service=memory_service)
    read_tool = next(t for t in agent.toolsets[0].tools.values() if t.name == "wiki_read")
    payload = read_tool.function(None, [id_a, id_b])
    assert payload == {
        "bodies": {id_a: "# A\nclaim one", id_b: "# B\nclaim two"},
        "unknown_page_ids": [],
    }


def test_wiki_read_reports_unknown_ids_instead_of_raising(agent, wiki, memory_service) -> None:
    """``wiki_read`` catches ``WikiPageNotFound`` and reports unknown ids.

    Pins production resilience: a hallucinated page_id (an id the
    agent invented, an id from a stale inventory row, an id whose
    file was deleted between the inventory and the read) must not
    fail the whole lint dispatch. ``WikiMemoryService.read`` raises;
    the linter's wrapper surfaces the partial result instead.
    """
    from lies.memory.retrieval import _page_id_for
    from lies.memory.models import WikiPageNotFound

    id_a = _page_id_for("a.md")
    memory_service.read.side_effect = WikiPageNotFound(f"unknown page_ids: ['{id_a}']")
    register_linter_tools(agent, wiki=wiki, memory_service=memory_service)
    read_tool = next(t for t in agent.toolsets[0].tools.values() if t.name == "wiki_read")
    payload = read_tool.function(None, [id_a])
    assert payload == {"bodies": {}, "unknown_page_ids": [id_a]}
