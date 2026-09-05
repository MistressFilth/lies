"""Tests for the wiki://catalog and wiki://catalog/{slug} MCP resources."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lies.memory.catalog import open_catalog, upsert_page
from lies.memory.catalog_models import CatalogPage
from lies.wiki.wiki import Wiki


def _stub_wiki(wiki_root: Path) -> Wiki:
    """A Wiki with all five role roots pointing at ``wiki_root``'s parent.

    The catalog module only reads ``wiki.wiki_dir``; the four other role
    roots exist so the dataclass validates. The MCP server resolves
    wikis through :func:`lies.mcp.resolution.resolve_wiki`, which calls
    :meth:`Wiki.require`. We monkey-patch that path below so the stub
    is returned without ``Wiki.require`` ever checking the XDG data root.
    """
    parent = wiki_root.parent
    return Wiki(
        name="t",
        data_root=parent,
        config_root=parent / "config",
        cache_root=parent / "cache",
        state_root=parent / "state",
        runtime_root=parent / "runtime",
    )


@pytest.fixture
def wiki(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Wiki:
    """A stub wiki with two pre-seeded catalog rows.

    Builds ``tmp_path/test-wiki/wiki/claude-code/{a,b}.md`` so the catalog
    disk-walk sees real pages; then explicitly upserts two rows so the
    test does not depend on the first-open backfill walk.
    """
    wiki_root = tmp_path / "test-wiki" / "wiki"
    page_dir = wiki_root / "claude-code"
    page_dir.mkdir(parents=True)
    (page_dir / "a.md").write_text("---\ntitle: A\n---\n\n# A\n", encoding="utf-8")
    (page_dir / "b.md").write_text("---\ntitle: B\n---\n\n# B\n", encoding="utf-8")

    wiki = _stub_wiki(wiki_root)

    # Seed the catalog on the wiki-relative dir (not the data_root).
    conn = open_catalog(wiki)
    try:
        upsert_page(conn, CatalogPage(slug="claude-code/a", title="A", source_pkg="claude-code"))
        upsert_page(conn, CatalogPage(slug="claude-code/b", title="B", source_pkg="claude-code"))
    finally:
        conn.close()

    monkeypatch.setenv("LIES_WIKI_NAME", "t")
    monkeypatch.setattr("lies.mcp.server.resolve_wiki", lambda _name=None: wiki)
    return wiki


def test_wiki_catalog_resource_returns_all_rows(wiki: Wiki) -> None:
    """wiki://catalog returns the structured list[dict] of catalog rows."""
    from lies.mcp.server import _wiki_catalog_impl

    payload = json.loads(_wiki_catalog_impl())
    assert isinstance(payload, list)
    assert {row["slug"] for row in payload} == {"claude-code/a", "claude-code/b"}
    # mode="json" should serialize the enum as its string value.
    assert all(row["section"] == "wiki" for row in payload)


def test_wiki_catalog_slug_resource_returns_single_row(wiki: Wiki) -> None:
    """wiki://catalog/{slug} returns a single dict for a known slug."""
    from lies.mcp.server import _wiki_catalog_slug_impl

    payload = json.loads(_wiki_catalog_slug_impl("claude-code/a"))
    assert payload["slug"] == "claude-code/a"
    assert payload["title"] == "A"
    assert payload["source_pkg"] == "claude-code"


def test_wiki_catalog_slug_resource_missing_returns_empty(wiki: Wiki) -> None:
    """wiki://catalog/{slug} returns "" when the slug is not present."""
    from lies.mcp.server import _wiki_catalog_slug_impl

    assert _wiki_catalog_slug_impl("missing/x") == ""
