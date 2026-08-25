"""MCP tool + resource parity for the JSONL receipt sidecar.

The ``wiki_changes`` tool returns ``MemoryPlanRecord`` dicts; the
``wiki://memory-changes`` resource renders the same data as formatted
text mirroring ``lies memory``. Tests patch ``lies.mcp.server.resolve_wiki``
so they don't depend on the XDG role-roots autouse fixture.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lies.memory import sidecar
from lies.memory.models import MemoryPlan, PageCreate
from lies.wiki.wiki import Wiki


def _wiki(tmp_path: Path) -> Wiki:
    """Build a 6-field Wiki rooted at ``tmp_path``; only ``data_root`` matters.

    Mirrors ``tests/unit/memory/test_sidecar.py:_wiki`` so the sidecar
    code can resolve ``<data_root>/.lies/memory_plans.jsonl``. The
    dataclass requires all six fields; ``wiki_dir`` is a derived
    property, not a constructor arg.
    """
    return Wiki(
        name="t",
        data_root=tmp_path,
        config_root=tmp_path / "config",
        cache_root=tmp_path / "cache",
        state_root=tmp_path / "state",
        runtime_root=tmp_path / "runtime",
    )


@pytest.fixture
def wiki(tmp_path: Path) -> Wiki:
    """A wiki with one seeded sidecar row."""
    w = _wiki(tmp_path)
    (tmp_path / ".lies").mkdir(parents=True, exist_ok=True)
    plan = MemoryPlan(
        rationale="create entity page",
        operations=[
            PageCreate(path="wiki/entities/p.md", content="# P", evidence=["page-1"]),
        ],
        evidence=["page-1"],
    )
    sidecar.append_receipt(w, plan, commit_sha="a1b2c3d4" + "0" * 32)
    return w


def test_wiki_changes_returns_records(monkeypatch: pytest.MonkeyPatch, wiki: Wiki) -> None:
    """The tool returns the seeded record as a dict."""
    monkeypatch.setenv("LIES_WIKI_NAME", "t")
    from lies.mcp import server

    monkeypatch.setattr("lies.mcp.server.resolve_wiki", lambda _name=None: wiki)
    # FastMCP 3.4.5 in ``decorator_mode='function'`` returns the
    # underlying function directly (no ``.fn`` wrapper); calling the
    # decorated name is the documented test idiom in this repo (see
    # tests/mcp/test_tools.py).
    records = server.wiki_changes(limit=10)
    assert len(records) == 1
    assert records[0]["rationale"] == "create entity page"
    assert records[0]["commit_sha"]


def test_wiki_changes_filters(monkeypatch: pytest.MonkeyPatch, wiki: Wiki) -> None:
    """The ``page`` filter is a substring match; non-matching page yields []."""
    monkeypatch.setenv("LIES_WIKI_NAME", "t")
    from lies.mcp import server

    monkeypatch.setattr("lies.mcp.server.resolve_wiki", lambda _name=None: wiki)
    records = server.wiki_changes(limit=10, page="nonexistent")
    assert records == []


def test_wiki_changes_returns_empty_on_missing_sidecar(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When no sidecar exists, the tool returns an empty list."""
    w = _wiki(tmp_path)
    monkeypatch.setenv("LIES_WIKI_NAME", "t")
    from lies.mcp import server

    monkeypatch.setattr("lies.mcp.server.resolve_wiki", lambda _name=None: w)
    assert server.wiki_changes(limit=10) == []


def test_wiki_memory_changes_resource_text(monkeypatch: pytest.MonkeyPatch, wiki: Wiki) -> None:
    """The resource renders formatted text mirroring ``lies memory``."""
    monkeypatch.setenv("LIES_WIKI_NAME", "t")
    from lies.mcp import server

    monkeypatch.setattr("lies.mcp.server.resolve_wiki", lambda _name=None: wiki)
    text = server.wiki_memory_changes()
    assert "create entity page" in text
