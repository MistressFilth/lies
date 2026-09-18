"""Tests for the ``lies wiki provenance`` CLI command (F29)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lies.cli.wiki import wiki_app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def wiki(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Bootstrap a wiki + catalog with no pages. Tests seed via _seed_pages.

    Returns the wiki_dir path for tests that still want to touch files
    (the slug-validation tests don't need any catalog state).
    """
    from lies.wiki.wiki import Wiki

    data_root = tmp_path / "test-wiki"
    wiki_root = data_root / "wiki"
    wiki_root.mkdir(parents=True)
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


def _seed_pages(wiki_root: Path) -> None:
    """Write catalog rows directly so ``derived_from`` is preserved.

    On-disk markdown files alone don't propagate ``derived_from`` to the
    catalog (the catalog rebuild walks the FS but doesn't parse the
    YAML frontmatter list — see ``catalog.rebuild_from_disk``). Tests
    that need a non-empty provenance view must upsert the catalog row
    with an explicit ``derived_from`` string.
    """
    from lies.memory.catalog import open_catalog, upsert_page
    from lies.memory.catalog_models import CatalogPage

    # Build a stub wiki that only exposes wiki_dir — open_catalog only
    # needs that attribute.
    class _StubWiki:
        pass

    w = _StubWiki()
    w.wiki_dir = wiki_root
    conn = open_catalog(w)
    try:
        upsert_page(
            conn,
            CatalogPage(
                slug="claude-code/concepts/hooks",
                title="Hooks",
                type="concept",
            ),
        )
        upsert_page(
            conn,
            CatalogPage(
                slug="claude-code/concepts/skills",
                title="Skills",
                type="concept",
            ),
        )
        upsert_page(
            conn,
            CatalogPage(
                slug="claude-code/synthesis/synthesis-of-hooks",
                title="Synthesis of Hooks",
                type="synthesis",
                derived_from="claude-code/concepts/hooks,claude-code/concepts/skills",
            ),
        )
    finally:
        conn.close()


def test_default_emits_tsv(runner: CliRunner, wiki: Path) -> None:
    _seed_pages(wiki)
    result = runner.invoke(wiki_app, [])
    assert result.exit_code == 0, result.output
    assert "claude-code/synthesis/synthesis-of-hooks" in result.output
    # Source pages must NOT appear (no derived_from).
    assert "claude-code/concepts/hooks\t" not in result.output
    # CSV-shaped derived_from in the trailing column.
    assert result.output.endswith("claude-code/concepts/hooks,claude-code/concepts/skills\n")


def test_json_emits_array(runner: CliRunner, wiki: Path) -> None:
    _seed_pages(wiki)
    result = runner.invoke(wiki_app, ["--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert isinstance(payload, list)
    assert len(payload) == 1
    assert payload[0]["slug"] == "claude-code/synthesis/synthesis-of-hooks"
    assert payload[0]["derived_from"] == [
        "claude-code/concepts/hooks",
        "claude-code/concepts/skills",
    ]


def test_page_emits_json_filter(runner: CliRunner, wiki: Path) -> None:
    _seed_pages(wiki)
    result = runner.invoke(
        wiki_app,
        ["--page", "claude-code/synthesis/synthesis-of-hooks"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert len(payload) == 1
    assert payload[0]["slug"] == "claude-code/synthesis/synthesis-of-hooks"


def test_page_missing_exits_2(runner: CliRunner, wiki: Path) -> None:
    _seed_pages(wiki)
    result = runner.invoke(wiki_app, ["--page", "claude-code/synthesis/missing"])
    assert result.exit_code == 2
    assert "error: page claude-code/synthesis/missing not found" in result.output


def test_page_with_slash_passes_validation(runner: CliRunner, wiki: Path) -> None:
    """Catalog slugs are path-shaped (``a/b/c``); slashes are legal."""
    result = runner.invoke(
        wiki_app,
        ["--page", "embedded/slash"],
    )
    # Slug passes validation; catalog lookup finds nothing; exit 2 with not-found.
    assert result.exit_code == 2
    assert "error: page embedded/slash not found" in result.output


def test_page_existing_source_page_emits_empty_provenance(runner: CliRunner, wiki: Path) -> None:
    """Regression: an existing source page (no derived_from) must NOT
    be reported as 'not found'. ``list_provenance_pages`` only returns
    rows with non-empty derived_from, so a source page yields an
    empty array — the CLI distinguishes "exists but not synthesised"
    from "doesn't exist" by checking catalog membership first.
    """
    from lies.memory.catalog import open_catalog, upsert_page
    from lies.memory.catalog_models import CatalogPage

    class _StubWiki:
        pass

    w = _StubWiki()
    w.wiki_dir = wiki
    conn = open_catalog(w)
    try:
        upsert_page(
            conn,
            CatalogPage(
                slug="claude-code/concepts/hooks",
                title="Hooks",
                type="concept",
            ),
        )
    finally:
        conn.close()

    result = runner.invoke(wiki_app, ["--page", "claude-code/concepts/hooks"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == []


def test_page_tab_in_slug_rejected(runner: CliRunner, wiki: Path) -> None:
    """Tabs in a slug would break the TSV renderer (extra column)."""
    result = runner.invoke(wiki_app, ["--page", "bad\tslug"])
    assert result.exit_code == 2
    assert "invalid page slug" in result.output


def test_page_newline_in_slug_rejected(runner: CliRunner, wiki: Path) -> None:
    """Newlines in a slug would break the TSV renderer (extra row)."""
    result = runner.invoke(wiki_app, ["--page", "bad\nslug"])
    assert result.exit_code == 2
    assert "invalid page slug" in result.output


def test_page_carriage_return_in_slug_rejected(runner: CliRunner, wiki: Path) -> None:
    """CR in a slug would break the TSV renderer (extra row)."""
    result = runner.invoke(wiki_app, ["--page", "bad\rslug"])
    assert result.exit_code == 2
    assert "invalid page slug" in result.output


def test_page_empty_after_trim_exits_2(runner: CliRunner, wiki: Path) -> None:
    result = runner.invoke(wiki_app, ["--page", "   "])
    assert result.exit_code == 2
    assert "error: invalid page slug: '   '" in result.output


def test_orphan_filters_to_dangling_only(runner: CliRunner, wiki: Path) -> None:
    """Add a synthesis page that cites a non-existent slug; --orphan shows it."""
    _seed_pages(wiki)
    from lies.memory.catalog import open_catalog, upsert_page
    from lies.memory.catalog_models import CatalogPage

    class _StubWiki:
        pass

    w = _StubWiki()
    w.wiki_dir = wiki
    conn = open_catalog(w)
    try:
        upsert_page(
            conn,
            CatalogPage(
                slug="claude-code/synthesis/orphan",
                title="Orphan",
                type="synthesis",
                derived_from="claude-code/concepts/skills,claude-code/concepts/missing",
            ),
        )
    finally:
        conn.close()

    result = runner.invoke(wiki_app, ["--orphan"])
    assert result.exit_code == 0, result.output
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert len(lines) == 1
    assert "orphan" in lines[0]
    assert "claude-code/concepts/missing" in lines[0]


def test_orphan_clean_page_returns_empty(runner: CliRunner, wiki: Path) -> None:
    _seed_pages(wiki)
    result = runner.invoke(
        wiki_app,
        [
            "--page",
            "claude-code/synthesis/synthesis-of-hooks",
            "--orphan",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == []


def test_no_synthesised_pages_returns_empty(runner: CliRunner, wiki: Path) -> None:
    """Wiki with only source pages — empty TSV, exit 0."""
    from lies.memory.catalog import open_catalog, upsert_page
    from lies.memory.catalog_models import CatalogPage

    class _StubWiki:
        pass

    w = _StubWiki()
    w.wiki_dir = wiki
    conn = open_catalog(w)
    try:
        upsert_page(
            conn,
            CatalogPage(
                slug="claude-code/concepts/hooks",
                title="Hooks",
                type="concept",
            ),
        )
    finally:
        conn.close()

    result = runner.invoke(wiki_app, [])
    assert result.exit_code == 0, result.output
    # typer.echo adds a trailing newline even on empty input.
    assert result.output.strip() == ""


def test_no_synthesised_pages_json_returns_empty_array(runner: CliRunner, wiki: Path) -> None:
    (wiki / "claude-code" / "concepts").mkdir(parents=True, exist_ok=True)
    (wiki / "claude-code" / "concepts" / "hooks.md").write_text(
        "---\ntitle: Hooks\ntype: concept\n---\n", encoding="utf-8"
    )
    result = runner.invoke(wiki_app, ["--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == []
