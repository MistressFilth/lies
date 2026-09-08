from collections.abc import Iterator
from pathlib import Path
import pytest
from lies.library.catalog import (
    LibraryCatalogPage,
    open_catalog,
    upsert_page,
    upsert_pages,
    remove_page,
    list_pages,
    list_slugs,
    SCHEMA_VERSION,
)
from lies.library.paths import Library


@pytest.fixture
def lib(tmp_path: Path, monkeypatch) -> Iterator[Library]:
    from lies import xdg

    monkeypatch.setattr(xdg, "data_home", lambda: tmp_path)
    Library.open.cache_clear()
    yield Library.open()


def test_schema_version_stamped(lib: Library) -> None:
    conn = open_catalog(lib)
    try:
        row = conn.execute("SELECT version FROM schema_version").fetchone()
        assert row["version"] == SCHEMA_VERSION == 2
    finally:
        conn.close()


def test_section_check_accepts_library(lib: Library) -> None:
    conn = open_catalog(lib)
    try:
        upsert_page(
            conn,
            LibraryCatalogPage(
                slug="x",
                title="t",
                type="",
                source_pkg="claude",
                section="library",
                updated="2024-01-01",
                hash="",
                derived_from="",
            ),
        )
        slugs = list_slugs(conn)
        assert "x" in slugs
    finally:
        conn.close()


def test_section_check_accepts_library_migrated(lib: Library) -> None:
    conn = open_catalog(lib)
    try:
        upsert_page(
            conn,
            LibraryCatalogPage(
                slug="x",
                title="t",
                type="",
                source_pkg="claude",
                section="library-migrated",
                updated="2024-01-01",
                hash="",
                derived_from="",
            ),
        )
        conn.commit()
        rows = list_pages(conn, section="library-migrated")
        assert len(rows) == 1
    finally:
        conn.close()


def test_upsert_replaces_existing(lib: Library) -> None:
    conn = open_catalog(lib)
    try:
        upsert_page(
            conn,
            LibraryCatalogPage(
                slug="x",
                title="first",
                type="",
                source_pkg="p",
                section="library",
                updated="2024-01-01",
                hash="h1",
                derived_from="",
            ),
        )
        conn.commit()
        upsert_page(
            conn,
            LibraryCatalogPage(
                slug="x",
                title="second",
                type="",
                source_pkg="p",
                section="library",
                updated="2024-01-02",
                hash="h2",
                derived_from="",
            ),
        )
        conn.commit()
        rows = list_pages(conn)
        assert len(rows) == 1
        assert rows[0].title == "second"
    finally:
        conn.close()


def test_upsert_pages_bulk(lib: Library) -> None:
    conn = open_catalog(lib)
    try:
        upsert_pages(
            conn,
            [
                LibraryCatalogPage(
                    slug=f"x{i}",
                    title=f"t{i}",
                    type="",
                    source_pkg="p",
                    section="library",
                    updated="2024-01-01",
                    hash="",
                    derived_from="",
                )
                for i in range(5)
            ],
        )
        conn.commit()
        assert len(list_slugs(conn)) == 5
    finally:
        conn.close()


def test_remove_page(lib: Library) -> None:
    conn = open_catalog(lib)
    try:
        upsert_page(
            conn,
            LibraryCatalogPage(
                slug="x",
                title="t",
                type="",
                source_pkg="p",
                section="library",
                updated="",
                hash="",
                derived_from="",
            ),
        )
        conn.commit()
        remove_page(conn, "x")
        conn.commit()
        assert "x" not in list_slugs(conn)
    finally:
        conn.close()
