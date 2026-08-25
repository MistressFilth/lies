"""Unit tests for ``lies memory`` subcommand flags + reconcile + truncate."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from lies.cli import app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _wiki(tmp_path: Path):
    """Build a minimal Wiki rooted at ``tmp_path``; only ``data_root`` matters.

    Mirrors the helper in ``tests/unit/memory/test_sidecar.py`` so the
    sidecar code can resolve ``<data_root>/.lies/memory_plans.jsonl``.
    Replicated here to keep the test file self-contained.
    """
    from lies.wiki.wiki import Wiki

    return Wiki(
        name="t",
        data_root=tmp_path,
        config_root=tmp_path / "config",
        cache_root=tmp_path / "cache",
        state_root=tmp_path / "state",
        runtime_root=tmp_path / "runtime",
    )


@pytest.fixture
def wiki_with_three_rows(tmp_path: Path, monkeypatch) -> None:
    """Create a wiki in tmp_path with three pre-seeded sidecar rows."""
    from lies.memory import sidecar
    from lies.memory.models import MemoryPlan, PageCreate

    wiki = _wiki(tmp_path)
    (tmp_path / ".lies").mkdir(parents=True, exist_ok=True)

    for i in range(3):
        plan = MemoryPlan(
            rationale=f"plan {i}",
            operations=[
                PageCreate(
                    path=f"entities/p{i}.md",
                    content=f"# P{i}",
                    evidence=["page-1"],
                ),
            ],
            evidence=["page-1"],
        )
        sidecar.append_receipt(wiki, plan, commit_sha=f"{i:040x}")
    monkeypatch.setenv("LIES_WIKI_NAME", "t")
    monkeypatch.setattr("lies.cli.resolve_wiki", lambda _name=None: wiki)
    return wiki


def test_memory_default_shows_last_10(runner, wiki_with_three_rows) -> None:
    result = runner.invoke(app, ["memory"])
    assert result.exit_code == 0
    assert "plan 2" in result.output
    assert "plan 1" in result.output
    assert "plan 0" in result.output


def test_memory_limit_caps_output(runner, wiki_with_three_rows) -> None:
    result = runner.invoke(app, ["memory", "--limit", "1"])
    assert result.exit_code == 0
    assert "plan 2" in result.output
    assert "plan 0" not in result.output


def test_memory_pages_filter(runner, wiki_with_three_rows) -> None:
    result = runner.invoke(app, ["memory", "--pages", "entities/p1.md"])
    assert result.exit_code == 0
    assert "plan 1" in result.output
    assert "plan 0" not in result.output


def test_memory_json_outputs_raw_jsonl(runner, wiki_with_three_rows) -> None:
    result = runner.invoke(app, ["memory", "--json", "--limit", "2"])
    assert result.exit_code == 0
    lines = [ln for ln in result.output.strip().splitlines() if ln.startswith("{")]
    assert len(lines) == 2
    rec = json.loads(lines[0])
    assert "commit_sha" in rec
    assert "rationale" in rec


def test_memory_reconcile_calls_reconcile(runner, wiki_with_three_rows) -> None:
    wiki = wiki_with_three_rows
    with patch("lies.cli.memory.sidecar.reconcile_from_git_log", return_value=3) as m:
        result = runner.invoke(app, ["memory", "reconcile"])
    assert result.exit_code == 0
    m.assert_called_once_with(wiki)


def test_memory_truncate_refuses_keep_zero(runner, wiki_with_three_rows) -> None:
    result = runner.invoke(app, ["memory", "truncate", "--keep", "0"])
    assert result.exit_code != 0
    assert "must be positive" in result.output


def test_memory_truncate_refuses_overcount_without_force(runner, wiki_with_three_rows) -> None:
    result = runner.invoke(app, ["memory", "truncate", "--keep", "10"])
    assert result.exit_code != 0
    assert "--force" in result.output
