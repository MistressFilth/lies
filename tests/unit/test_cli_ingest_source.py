"""Tests for the ``ingest_source`` CLI subcommand and its ``--no-llm`` opt-out.

The default path (no flag) routes through ``Orchestrator.run_ingest`` with
``no_llm=False`` (full LLM round-trip via the page-writer agent). The
``--no-llm`` flag demotes to the legacy ``sync_collection`` shim and emits
a stderr informational line so operators know the LLM-shaped distillation
was skipped.
"""

from __future__ import annotations

from typing import Any

import pytest
from typer.testing import CliRunner

from lies.cli import app

runner = CliRunner()


class _FakeOrchestrator:
    """Stand-in for ``Orchestrator`` that records the kwargs passed to ``run_ingest``.

    The CLI references ``Orchestrator`` as a bare name; tests monkeypatch
    the module-level lookup with a factory that returns an instance of
    this recorder. ``run_ingest`` returns a deterministic string so the
    CLI's ``typer.echo(output)`` path can be exercised end-to-end.
    """

    def __init__(self, wiki: Any, recorder: dict[str, object]) -> None:
        self.wiki = wiki
        self.recorder = recorder

    def run_ingest(self, source: str, *, no_llm: bool = False) -> str:
        self.recorder["source"] = source
        self.recorder["no_llm"] = no_llm
        return f"fake-ingested {source}"


def test_ingest_source_default_runs_llm_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default invocation (no flag) keeps the LLM round-trip (``no_llm=False``)."""
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        "lies.cli.ingestion.Orchestrator",
        lambda wiki: _FakeOrchestrator(wiki, recorder=seen),
    )
    result = runner.invoke(
        app,
        ["ingest-source", "raw/x.md", "--collection", "foo"],
    )
    assert result.exit_code == 0, (
        f"expected exit 0; got {result.exit_code}; stderr={result.stderr!r}"
    )
    assert seen.get("source") == "raw/x.md"
    assert seen.get("no_llm") is False


def test_ingest_source_no_llm_flag_demotes_to_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--no-llm`` forwards ``no_llm=True`` and emits a stderr notice."""
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        "lies.cli.ingestion.Orchestrator",
        lambda wiki: _FakeOrchestrator(wiki, recorder=seen),
    )
    result = runner.invoke(
        app,
        ["ingest-source", "raw/x.md", "--collection", "foo", "--no-llm"],
    )
    assert result.exit_code == 0, (
        f"expected exit 0; got {result.exit_code}; stderr={result.stderr!r}"
    )
    assert seen.get("source") == "raw/x.md"
    assert seen.get("no_llm") is True
    assert "sync_collection" in (result.stderr or "")


def test_ingest_source_help_describes_no_llm_opt_out() -> None:
    """``--help`` documents the ``--no-llm`` flag's opt-out semantics."""
    result = runner.invoke(app, ["ingest-source", "--help"])
    assert result.exit_code == 0
    assert "--no-llm" in result.stdout
