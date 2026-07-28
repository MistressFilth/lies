"""Tests for the LIES MCP server tools.

Each tool is tested by calling the decorated function directly after
registering the server module. The Orchestrator's agent is mocked so
no real LLM call is made — same pattern as
``tests/integration/test_end_to_end.py``.
"""
from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from lies import __version__
from lies.mcp.server import (
    SynthesizedMcpAnswer,
    ingest_source,
    init_wiki,
    lint,
    mcp,
    query,
)
from lies.orchestrator import Orchestrator
from lies.query.models import SynthesizedAnswer

# Each test gets a tmp_path and the env-reset fixture from conftest.py.


def test_init_wiki_creates_wiki_structure(tmp_path: Path) -> None:
    target = tmp_path / "new-wiki"
    out = init_wiki(str(target))
    assert f"lies {__version__}" not in out  # sanity — not the version string
    assert "Initialized" in out
    assert target.is_dir()
    assert (target / "wiki").is_dir()
    assert (target / "raw").is_dir()
    assert (target / ".lies").is_dir()
    assert (target / ".lies" / "schema.md").is_file()
    # Git is initialized and has at least the initial commit.
    layout = (target / ".git").exists()  # sanity
    assert layout


def test_init_wiki_rejects_non_empty_target(tmp_path: Path) -> None:
    target = tmp_path / "existing"
    target.mkdir()
    (target / "stuff").write_text("x", encoding="utf-8")
    with pytest.raises(Exception, match="not empty"):
        init_wiki(str(target))


def test_ingest_source_returns_agent_output(sample_wiki) -> None:
    """ingest_source delegates to Orchestrator.run_ingest and returns its output."""
    # ``sample_wiki`` is already a fully-initialized wiki (git repo, fixture
    # content committed) rooted at a tmp path; that is the wiki_root we
    # pass to ingest_source.
    with mock.patch.object(Orchestrator, "_build", lambda self: None), \
         mock.patch.object(Orchestrator, "run_ingest", autospec=True) as mock_run_ingest:
        mock_run_ingest.return_value = "ingested fixture-entity"
        out = ingest_source(
            "raw/articles/sample-article.md",
            wiki_root=str(sample_wiki.root),
        )

    assert out == "ingested fixture-entity"


def test_query_returns_synthesized_mcp_answer(sample_wiki, monkeypatch: pytest.MonkeyPatch) -> None:
    """query returns a SynthesizedMcpAnswer with the right three fields."""
    monkeypatch.setenv("LIES_WIKI_ROOT", str(sample_wiki.root))

    with mock.patch.object(Orchestrator, "_build", lambda self: None), \
         mock.patch.object(Orchestrator, "run_query", autospec=True) as mock_run_query:
        mock_run_query.return_value = SynthesizedAnswer(
            answer="### What is MVCC?\n\nA protocol.",
            citations=["entities/postgres.md"],
            pages_read=["entities/postgres.md"],
            fallback_used=False,
            fallback_reason="",
            page_links=["[Postgres](entities/postgres.md)"],
        )
        result = query("What is MVCC?")

    assert isinstance(result, SynthesizedMcpAnswer)
    assert result.answer.startswith("### What is MVCC?")
    assert result.fallback_used is False
    assert result.fallback_reason is None  # mapped from empty string


def test_query_propagates_fallback_reason(sample_wiki, monkeypatch: pytest.MonkeyPatch) -> None:
    """A fallback answer surfaces fallback_used and fallback_reason."""
    monkeypatch.setenv("LIES_WIKI_ROOT", str(sample_wiki.root))

    with mock.patch.object(Orchestrator, "_build", lambda self: None), \
         mock.patch.object(Orchestrator, "run_query", autospec=True) as mock_run_query:
        mock_run_query.return_value = SynthesizedAnswer(
            answer="### Fallback answer",
            fallback_used=True,
            fallback_reason="qmd_unavailable",
        )
        result = query("anything")

    assert result.fallback_used is True
    assert result.fallback_reason == "qmd_unavailable"


def test_lint_returns_markdown_report(sample_wiki, monkeypatch: pytest.MonkeyPatch) -> None:
    """lint delegates to Orchestrator.run_lint and returns the report."""
    monkeypatch.setenv("LIES_WIKI_ROOT", str(sample_wiki.root))

    fake_report = "## Lint report\n\n_No findings._\n"
    with mock.patch.object(Orchestrator, "_build", lambda self: None), \
         mock.patch.object(Orchestrator, "run_lint", autospec=True) as mock_run_lint:
        mock_run_lint.return_value = fake_report
        out = lint()

    assert out == fake_report


def test_mcp_server_has_correct_name() -> None:
    """The FastMCP instance is named 'lies'."""
    assert mcp.name == "lies"
