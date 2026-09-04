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

    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()
    page_dir = wiki_dir / "x" / "concepts"
    page_dir.mkdir(parents=True)
    (page_dir / "a.md").write_text("---\ntitle: A\n---\n\n# A\n", encoding="utf-8")
    (page_dir / "b.md").write_text("---\ntitle: B\n---\n\n# B\n", encoding="utf-8")
    # Top-level system files (production layout: at the root of wiki_dir).
    (wiki_dir / "log.md").write_text("# Log\n", encoding="utf-8")
    (wiki_dir / "index.md").write_text("# Index\n", encoding="utf-8")

    class _StubWiki:
        pass

    wiki = _StubWiki()
    wiki.wiki_dir = wiki_dir  # type: ignore[attr-defined]

    pages = rebuild_from_disk(wiki)
    slugs = {p.slug for p in pages}
    assert "x/concepts/a" in slugs
    assert "x/concepts/b" in slugs
    assert "log" not in slugs
    assert "index" not in slugs


def test_rebuild_from_disk_keeps_nested_system_named_files(tmp_path: Path) -> None:
    """A nested ``concepts/index.md`` is a real wiki page, not a system file.

    Guards against the Task 3 review finding: the previous basename-based
    filter (``md.name in {"index.md", ...}``) silently dropped a legit
    ``<collection>/concepts/index.md`` even though only the top-level
    ``wiki/index.md`` is a system artifact.
    """
    from lies.memory.catalog import rebuild_from_disk

    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()
    page_dir = wiki_dir / "x" / "concepts"
    page_dir.mkdir(parents=True)
    (page_dir / "index.md").write_text("---\ntitle: Index\n---\n\n# Index\n", encoding="utf-8")
    (page_dir / "overview.md").write_text(
        "---\ntitle: Overview\n---\n\n# Overview\n", encoding="utf-8"
    )
    # Top-level system files still excluded.
    (wiki_dir / "index.md").write_text("# Top Index\n", encoding="utf-8")
    (wiki_dir / "overview.md").write_text("# Top Overview\n", encoding="utf-8")

    class _StubWiki:
        pass

    wiki = _StubWiki()
    wiki.wiki_dir = wiki_dir  # type: ignore[attr-defined]

    pages = rebuild_from_disk(wiki)
    slugs = {p.slug for p in pages}
    assert "x/concepts/index" in slugs
    assert "x/concepts/overview" in slugs
    # The top-level system files are NOT in the catalog — only nested
    # ones with the same name. (Top-level slugs would be ``"index"`` /
    # ``"overview"``, not the nested ones.)
    assert "index" not in slugs
    assert "overview" not in slugs


def test_reconcile_dry_run(tmp_path: Path) -> None:
    """dry_run reports would_add/would_remove without writing."""
    from lies.memory.catalog import open_catalog, reconcile, upsert_page
    from lies.memory.catalog_models import CatalogPage

    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()
    (wiki_dir / "on-disk.md").write_text("# On-disk\n", encoding="utf-8")

    class _StubWiki:
        pass

    wiki = _StubWiki()
    wiki.wiki_dir = wiki_dir  # type: ignore[attr-defined]

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

    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()
    (wiki_dir / "on-disk.md").write_text("# On-disk\n", encoding="utf-8")

    class _StubWiki:
        pass

    wiki = _StubWiki()
    wiki.wiki_dir = wiki_dir  # type: ignore[attr-defined]

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

    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()
    (wiki_dir / "a.md").write_text("# A\n", encoding="utf-8")

    class _StubWiki:
        pass

    wiki = _StubWiki()
    wiki.wiki_dir = wiki_dir  # type: ignore[attr-defined]

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
    # Full link line — guards against the doubled ``wiki/<pkg>/<pkg>/...``
    # segment bug found in Task 3 review. Source_pkg is already encoded
    # in the slug, so the link is just ``{slug}.md``.
    assert "- [Hooks](claude-code/concepts/hooks.md)" in md
    assert "- [Skills](claude-code/concepts/skills.md)" in md
    # No summary line — title only.
    assert "— " not in md


def test_render_markdown_empty(tmp_path: Path, conn: sqlite3.Connection) -> None:
    from lies.memory.catalog import render_markdown

    md = render_markdown(conn)
    assert md.startswith("# Wiki Catalog")
    assert "- " not in md


def test_slug_for_bare_path() -> None:
    """Bare slug → returned verbatim (no suffix, no prefix)."""
    from lies.memory.catalog_models import _slug_for

    assert _slug_for("claude-code/concepts/hooks") == "claude-code/concepts/hooks"


def test_slug_for_md_suffixed_path() -> None:
    """.md suffix is stripped; prefix (if any) is preserved."""
    from lies.memory.catalog_models import _slug_for

    assert _slug_for("claude-code/concepts/hooks.md") == "claude-code/concepts/hooks"


def test_slug_for_wiki_prefixed_path() -> None:
    """Leading ``wiki/`` prefix is stripped (single occurrence)."""
    from lies.memory.catalog_models import _slug_for

    assert _slug_for("wiki/claude-code/concepts/hooks") == "claude-code/concepts/hooks"
    assert _slug_for("wiki/claude-code/concepts/hooks.md") == "claude-code/concepts/hooks"


def test_slug_for_double_prefixed_path() -> None:
    """Defensive double-strip handles ``wiki/wiki/...`` (e.g. page-writer bug)."""
    from lies.memory.catalog_models import _slug_for

    assert _slug_for("wiki/wiki/claude-code/concepts/hooks") == "claude-code/concepts/hooks"
    assert _slug_for("wiki/wiki/claude-code/concepts/hooks.md") == "claude-code/concepts/hooks"
