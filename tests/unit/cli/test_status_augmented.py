"""Unit tests for `lies status` augmented output."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from lies.cli import app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def wiki_with_rows(tmp_path: Path, monkeypatch):
    from lies.memory import sidecar
    from lies.memory.models import MemoryPlan, PageCreate
    from lies.wiki.wiki import Wiki

    wiki = Wiki(
        name="t",
        data_root=tmp_path,
        config_root=tmp_path / "config",
        cache_root=tmp_path / "cache",
        state_root=tmp_path / "state",
        runtime_root=tmp_path / "runtime",
    )
    (tmp_path / "wiki").mkdir()
    (tmp_path / ".lies").mkdir()
    plan = MemoryPlan(
        rationale="create entity page for Postgres from query '...'",
        operations=[
            PageCreate(path="wiki/entities/postgres.md", content="# P", evidence=["page-1"]),
        ],
        evidence=["page-1"],
    )
    sidecar.append_receipt(wiki, plan, commit_sha="a1b2c3d4" + "0" * 32)
    monkeypatch.setenv("LIES_WIKI_NAME", "t")
    monkeypatch.setattr("lies.cli.resolve_wiki", lambda _name=None: wiki)
    return wiki


def test_status_includes_recent_writes_section(runner, wiki_with_rows) -> None:
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "recent invisible writes" in result.output
    assert "create entity page for Postgres" in result.output


def test_status_memory_limit_flag(runner, wiki_with_rows) -> None:
    result = runner.invoke(app, ["status", "--memory-limit", "0"])
    assert result.exit_code == 0
    assert "recent invisible writes" not in result.output


def test_status_handles_missing_sidecar_gracefully(runner, tmp_path, monkeypatch) -> None:
    from lies.wiki.wiki import Wiki

    wiki = Wiki(
        name="t",
        data_root=tmp_path,
        config_root=tmp_path / "config",
        cache_root=tmp_path / "cache",
        state_root=tmp_path / "state",
        runtime_root=tmp_path / "runtime",
    )
    (tmp_path / "wiki").mkdir()
    monkeypatch.setenv("LIES_WIKI_NAME", "t")
    monkeypatch.setattr("lies.cli.resolve_wiki", lambda _name=None: wiki)
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    # No sidecar → no section, no crash
    assert "recent invisible writes" not in result.output
