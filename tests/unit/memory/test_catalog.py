"""Tests for lies.memory.catalog (CRUD + open_catalog)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from lies.memory.catalog import (
    count_pages,
    get_page,
    list_pages,
    list_slugs,
    open_catalog,
    remove_page,
    remove_pages,
    slug_exists,
    upsert_page,
    upsert_pages,
)
from lies.memory.catalog_models import CatalogPage, PageSection


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    """A fresh catalog.db in tmp_path; auto-created on open."""

    # Pass a stub wiki object — open_catalog reads only `wiki_dir`.
    class _StubWiki:
        pass

    wiki = _StubWiki()
    wiki.wiki_dir = tmp_path  # type: ignore[attr-defined]
    return open_catalog(wiki)


def test_open_catalog_creates_schema(tmp_path: Path) -> None:
    class _StubWiki:
        pass

    wiki = _StubWiki()
    wiki.wiki_dir = tmp_path  # type: ignore[attr-defined]
    conn = open_catalog(wiki)
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='pages'"
        ).fetchone()
        assert row is not None
    finally:
        conn.close()


def test_open_catalog_sets_wal_and_busy_timeout(tmp_path: Path) -> None:
    class _StubWiki:
        pass

    wiki = _StubWiki()
    wiki.wiki_dir = tmp_path  # type: ignore[attr-defined]
    conn = open_catalog(wiki)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
        # busy_timeout returns the configured value (ms)
        timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert timeout == 5000
    finally:
        conn.close()


def test_open_catalog_idempotent(tmp_path: Path) -> None:
    class _StubWiki:
        pass

    wiki = _StubWiki()
    wiki.wiki_dir = tmp_path  # type: ignore[attr-defined]
    conn1 = open_catalog(wiki)
    conn1.close()
    conn2 = open_catalog(wiki)
    try:
        assert count_pages(conn2) == 0
    finally:
        conn2.close()


def test_upsert_page_inserts(tmp_path: Path, conn: sqlite3.Connection) -> None:
    page = CatalogPage(
        slug="claude-code/concepts/hooks",
        title="Hooks",
        type="concept",
        source_pkg="claude-code",
        updated="2026-09-04",
    )
    upsert_page(conn, page)
    row = get_page(conn, "claude-code/concepts/hooks")
    assert row is not None
    assert row.title == "Hooks"
    assert row.type == "concept"
    assert row.section is PageSection.wiki


def test_upsert_page_replaces_on_conflict(tmp_path: Path, conn: sqlite3.Connection) -> None:
    page = CatalogPage(slug="x", title="Old", type="concept")
    upsert_page(conn, page)
    upsert_page(conn, CatalogPage(slug="x", title="New", type="entity"))
    row = get_page(conn, "x")
    assert row.title == "New"
    assert row.type == "entity"


def test_upsert_pages_batched(tmp_path: Path, conn: sqlite3.Connection) -> None:
    pages = [CatalogPage(slug=f"x/{i}", title=f"T{i}") for i in range(5)]
    upsert_pages(conn, pages)
    assert count_pages(conn) == 5


def test_upsert_pages_empty_noop(tmp_path: Path, conn: sqlite3.Connection) -> None:
    upsert_pages(conn, [])
    assert count_pages(conn) == 0


def test_remove_page(tmp_path: Path, conn: sqlite3.Connection) -> None:
    upsert_page(conn, CatalogPage(slug="x", title="X"))
    assert remove_page(conn, "x") is True
    assert remove_page(conn, "x") is False  # already gone
    assert count_pages(conn) == 0


def test_remove_pages(tmp_path: Path, conn: sqlite3.Connection) -> None:
    for s in ["a", "b", "c"]:
        upsert_page(conn, CatalogPage(slug=s))
    assert remove_pages(conn, ["a", "c"]) == 2
    assert count_pages(conn) == 1
    assert slug_exists(conn, "b")


def test_list_pages_filters(tmp_path: Path, conn: sqlite3.Connection) -> None:
    upsert_pages(
        conn,
        [
            CatalogPage(slug="x/1", type="concept", source_pkg="x"),
            CatalogPage(slug="x/2", type="entity", source_pkg="x"),
            CatalogPage(slug="y/1", type="concept", source_pkg="y"),
        ],
    )
    by_type = list_pages(conn, page_type="concept")
    assert {p.slug for p in by_type} == {"x/1", "y/1"}
    by_pkg = list_pages(conn, source_pkg="x")
    assert {p.slug for p in by_pkg} == {"x/1", "x/2"}


def test_list_slugs(tmp_path: Path, conn: sqlite3.Connection) -> None:
    upsert_pages(conn, [CatalogPage(slug=f"s/{i}") for i in range(3)])
    assert list_slugs(conn) == {"s/0", "s/1", "s/2"}


# ---------------------------------------------------------------------------
# Disk sync (rebuild_from_disk, reconcile, render_markdown)
# ---------------------------------------------------------------------------


def test_rebuild_from_disk_walks_markdown(tmp_path: Path) -> None:
    """rebuild_from_disk walks wiki/**/*.md and returns CatalogPage rows."""
    from lies.memory.catalog import rebuild_from_disk

    wiki_root = tmp_path / "test-wiki"
    wiki_root.mkdir()
    page_dir = wiki_root / "wiki" / "x" / "concepts"
    page_dir.mkdir(parents=True)
    (page_dir / "a.md").write_text("---\ntitle: A\n---\n\n# A\n", encoding="utf-8")
    (page_dir / "b.md").write_text("---\ntitle: B\n---\n\n# B\n", encoding="utf-8")
    (wiki_root / "wiki" / "log.md").write_text("# Log\n", encoding="utf-8")
    (wiki_root / "wiki" / "index.md").write_text("# Index\n", encoding="utf-8")

    class _StubWiki:
        pass

    wiki = _StubWiki()
    wiki.wiki_dir = wiki_root  # type: ignore[attr-defined]

    pages = rebuild_from_disk(wiki)
    slugs = {p.slug for p in pages}
    assert "x/concepts/a" in slugs
    assert "x/concepts/b" in slugs
    assert "log" not in slugs
    assert "index" not in slugs


def test_reconcile_dry_run(tmp_path: Path) -> None:
    """dry_run reports would_add/would_remove without writing."""
    from lies.memory.catalog import open_catalog, reconcile, upsert_page
    from lies.memory.catalog_models import CatalogPage

    wiki_root = tmp_path / "test-wiki"
    wiki_root.mkdir()
    (wiki_root / "wiki").mkdir()
    (wiki_root / "wiki" / "on-disk.md").write_text("# On-disk\n", encoding="utf-8")

    class _StubWiki:
        pass

    wiki = _StubWiki()
    wiki.wiki_dir = wiki_root  # type: ignore[attr-defined]

    # Pre-seed an orphan row in catalog (file is missing)
    conn = open_catalog(wiki)
    try:
        upsert_page(conn, CatalogPage(slug="dangling", title="D"))
    finally:
        conn.close()

    result = reconcile(wiki, dry_run=True)
    assert result.would_add == 1  # on-disk.md has no row
    assert result.would_remove == 1  # "dangling" has no file


def test_reconcile_applies_changes(tmp_path: Path) -> None:
    """non-dry_run upserts orphans + removes dangling rows."""
    from lies.memory.catalog import get_page, open_catalog, reconcile, upsert_page
    from lies.memory.catalog_models import CatalogPage

    wiki_root = tmp_path / "test-wiki"
    wiki_root.mkdir()
    (wiki_root / "wiki").mkdir()
    (wiki_root / "wiki" / "on-disk.md").write_text("# On-disk\n", encoding="utf-8")

    class _StubWiki:
        pass

    wiki = _StubWiki()
    wiki.wiki_dir = wiki_root  # type: ignore[attr-defined]

    conn = open_catalog(wiki)
    try:
        upsert_page(conn, CatalogPage(slug="dangling", title="D"))
    finally:
        conn.close()

    result = reconcile(wiki, dry_run=False)
    assert result.added == 1
    assert result.removed == 1

    # Verify state after reconcile
    conn = open_catalog(wiki)
    try:
        assert get_page(conn, "dangling") is None
        assert get_page(conn, "on-disk") is not None
    finally:
        conn.close()


def test_reconcile_idempotent(tmp_path: Path) -> None:
    from lies.memory.catalog import reconcile

    wiki_root = tmp_path / "test-wiki"
    wiki_root.mkdir()
    (wiki_root / "wiki").mkdir()
    (wiki_root / "wiki" / "a.md").write_text("# A\n", encoding="utf-8")

    class _StubWiki:
        pass

    wiki = _StubWiki()
    wiki.wiki_dir = wiki_root  # type: ignore[attr-defined]

    first = reconcile(wiki, dry_run=False)
    second = reconcile(wiki, dry_run=False)
    assert first.added >= 1
    assert second.added == 0 and second.removed == 0


def test_render_markdown_title_only(tmp_path: Path, conn: sqlite3.Connection) -> None:
    """render_markdown emits title-only lines (closes P2 entry)."""
    from lies.memory.catalog import render_markdown

    upsert_pages(
        conn,
        [
            CatalogPage(slug="claude-code/concepts/hooks", title="Hooks", source_pkg="claude-code"),
            CatalogPage(
                slug="claude-code/concepts/skills", title="Skills", source_pkg="claude-code"
            ),
        ],
    )
    md = render_markdown(conn)
    assert "- [Hooks]" in md
    assert "- [Skills]" in md
    # No summary line — title only
    assert "— " not in md or md.count("— ") == 0


def test_render_markdown_empty(tmp_path: Path, conn: sqlite3.Connection) -> None:
    from lies.memory.catalog import render_markdown

    md = render_markdown(conn)
    assert md.startswith("# Wiki Catalog")
    assert "- " not in md
