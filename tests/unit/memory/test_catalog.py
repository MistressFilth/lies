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
