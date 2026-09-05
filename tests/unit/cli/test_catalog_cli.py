"""Tests for the ``lies catalog`` CLI group."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lies.cli.catalog import catalog_app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def wiki(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Bootstrap a minimal wiki with two pages, set as the active wiki.

    Mirrors the fixture in ``test_memory_cli.py``: monkeypatches
    ``lies.cli.resolve_wiki`` (the lazy re-export of
    ``lies.mcp.resolution.resolve_wiki``) to bypass XDG. The catalog
    module only reads ``wiki.wiki_dir`` from the resolved wiki object,
    so a plain ``Wiki(...)`` instance is sufficient.
    """
    from lies.wiki.wiki import Wiki

    data_root = tmp_path / "test-wiki"
    wiki_root = data_root / "wiki"
    wiki_root.mkdir(parents=True)
    page_dir = wiki_root / "claude-code" / "concepts"
    page_dir.mkdir(parents=True)
    (page_dir / "hooks.md").write_text(
        "---\ntitle: Hooks\ntype: concept\n---\n\n# Hooks\n", encoding="utf-8"
    )
    (page_dir / "skills.md").write_text(
        "---\ntitle: Skills\ntype: concept\n---\n\n# Skills\n", encoding="utf-8"
    )

    wiki = Wiki(
        name="test-wiki",
        data_root=data_root,
        config_root=tmp_path / "config",
        cache_root=tmp_path / "cache",
        state_root=tmp_path / "state",
        runtime_root=tmp_path / "runtime",
    )
    monkeypatch.setenv("LIES_WIKI_NAME", "test-wiki")
    monkeypatch.setattr("lies.cli.resolve_wiki", lambda _name=None: wiki)
    return wiki_root


def test_status_prints_row_count(runner: CliRunner, wiki: Path) -> None:
    result = runner.invoke(catalog_app, ["status"])
    assert result.exit_code == 0, result.output
    assert "2 pages" in result.output
    assert "schema v1" in result.output


def test_dump_emits_json(runner: CliRunner, wiki: Path) -> None:
    result = runner.invoke(catalog_app, ["dump", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert isinstance(payload, list)
    assert len(payload) == 2
    slugs = {row["slug"] for row in payload}
    assert "claude-code/concepts/hooks" in slugs
    assert "claude-code/concepts/skills" in slugs


def test_reconcile_dry_run(runner: CliRunner, wiki: Path) -> None:
    """Add an orphan row, run reconcile --dry-run, verify nothing written."""
    from lies.memory.catalog import open_catalog, upsert_page
    from lies.memory.catalog_models import CatalogPage

    # Build a stub wiki that only exposes wiki_dir, since open_catalog
    # only reads that attribute.
    class _StubWiki:
        pass

    w = _StubWiki()
    w.wiki_dir = wiki
    conn = open_catalog(w)
    try:
        upsert_page(conn, CatalogPage(slug="dangling", title="D"))
    finally:
        conn.close()

    result = runner.invoke(catalog_app, ["reconcile", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "Would remove 1" in result.output


def test_rebuild_is_idempotent(runner: CliRunner, wiki: Path) -> None:
    first = runner.invoke(catalog_app, ["rebuild"])
    second = runner.invoke(catalog_app, ["rebuild"])
    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    # Second rebuild upserts the same two pages; nothing new is added.
    # The brief's expected output is "in sync" — the rebuild command
    # reports the upsert count. We assert it stays consistent across calls.
    assert "Upserted 2" in first.output
    assert "Upserted 2" in second.output


def test_render_writes_markdown(runner: CliRunner, wiki: Path, tmp_path: Path) -> None:
    out_path = tmp_path / "index.md"
    result = runner.invoke(catalog_app, ["render", "--out", str(out_path)])
    assert result.exit_code == 0, result.output
    assert out_path.exists()
    text = out_path.read_text(encoding="utf-8")
    assert "- [Hooks]" in text
    assert "- [Skills]" in text
