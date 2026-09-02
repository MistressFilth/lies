"""Tests for the MCP ``ingest_source`` tool's ``no_llm`` opt-out.

The default path (``no_llm=False``) routes through ``Orchestrator.run_ingest``
which runs the LLM round-trip (source-reader + page-writer). ``no_llm=True``
demotes to ``sync_collection`` for callers that want the raw ETL pass
without an LLM round-trip, mirroring the CLI's ``--no-llm`` flag.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.conftest import make_wiki


class _FakeOrchestrator:
    """Stand-in for ``Orchestrator`` that records the kwargs passed to ``run_ingest``.

    The MCP server's ``ingest_source`` references ``Orchestrator`` as a
    module-level import (set up at import time); tests monkeypatch the
    lookup with a factory that returns an instance of this recorder.
    ``run_ingest`` returns a deterministic string so the MCP tool's
    return path is exercised end-to-end.
    """

    def __init__(self, wiki: Any, recorder: dict[str, object]) -> None:
        self.wiki = wiki
        self.recorder = recorder

    def run_ingest(self, source: str, *, no_llm: bool = False) -> str:
        self.recorder["source"] = source
        self.recorder["no_llm"] = no_llm
        return f"fake-ingested {source}"


@pytest.fixture
def wiki(tmp_path: Path):
    """A Wiki rooted at a tmp data_root (matches existing MCP test pattern)."""
    root = tmp_path / "wiki"
    root.mkdir()
    return make_wiki(name="mcp-no-llm", data_root=root)


def test_mcp_ingest_source_default_runs_llm_path(
    wiki: object,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Default invocation (no ``no_llm``) keeps the LLM round-trip (``no_llm=False``)."""
    monkeypatch.setenv("LIES_WIKI_NAME", "mcp-no-llm")
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        "lies.mcp.server.Orchestrator",
        lambda w: _FakeOrchestrator(w, recorder=seen),
    )
    monkeypatch.setattr("lies.collections.bootstrap.ensure_wiki", lambda name=None: wiki)

    from lies.mcp.server import ingest_source

    out = ingest_source(
        source="raw/x.md",
        collection="foo",
        name="mcp-no-llm",
    )
    assert out == "fake-ingested raw/x.md"
    assert seen.get("source") == "raw/x.md"
    assert seen.get("no_llm") is False


def test_mcp_ingest_source_no_llm_demotes_to_sync(
    wiki: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``no_llm=True`` demotes to ``sync_collection`` with the explicit collection."""
    monkeypatch.setenv("LIES_WIKI_NAME", "mcp-no-llm")
    seen: dict[str, object] = {}

    def _fake_sync(w: object, collection: str, *, force: bool = False) -> str:
        seen["called"] = (w, collection, force)
        return "synced"

    monkeypatch.setattr("lies.etl.sync_helper.sync_collection", _fake_sync)
    monkeypatch.setattr("lies.collections.bootstrap.ensure_wiki", lambda name=None: wiki)

    from lies.mcp.server import ingest_source

    out = ingest_source(
        source="raw/x.md",
        collection="foo",
        name="mcp-no-llm",
        no_llm=True,
    )
    assert seen.get("called") == (wiki, "foo", False)
    assert "no_llm" in out
    assert "foo" in out
