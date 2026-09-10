"""SQLite-backed library catalog. WAL + 5s busy timeout. v2 schema.

Differences from wiki-side ``memory.catalog``:

- CHECK constraint on ``section`` accepts ``library`` and
  ``library-migrated`` values (wiki only accepts ``wiki`` and
  ``ingested``).
- No seed-on-first-open (``rebuild_from_disk`` is wiki-specific);
  library starts empty.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

from lies.library.paths import Library

SCHEMA_VERSION = 2

_DDL = """\
CREATE TABLE IF NOT EXISTS pages (
    slug         TEXT PRIMARY KEY,
    title        TEXT NOT NULL DEFAULT '',
    type         TEXT NOT NULL DEFAULT '',
    source_pkg   TEXT NOT NULL DEFAULT '',
    section      TEXT NOT NULL DEFAULT 'library'
        CHECK(section IN ('library', 'library-migrated')),
    updated      TEXT NOT NULL DEFAULT '',
    hash         TEXT NOT NULL DEFAULT '',
    derived_from TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_pages_pkg     ON pages(source_pkg);
CREATE INDEX IF NOT EXISTS idx_pages_type    ON pages(type);
CREATE INDEX IF NOT EXISTS idx_pages_section ON pages(section);
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);
"""

_UPSERT_SQL = """\
INSERT INTO pages
    (slug, title, type, source_pkg, section, updated, hash, derived_from)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(slug) DO UPDATE SET
    title        = excluded.title,
    type         = excluded.type,
    source_pkg   = excluded.source_pkg,
    section      = excluded.section,
    updated      = excluded.updated,
    hash         = excluded.hash,
    derived_from = excluded.derived_from
"""


@dataclass(frozen=True)
class LibraryCatalogPage:
    slug: str
    title: str
    type: str
    source_pkg: str
    section: str
    updated: str
    hash: str
    derived_from: str


def open_catalog(library: Library) -> sqlite3.Connection:
    catalog_path = library.catalog_path
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(catalog_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(_DDL)
    conn.execute("DELETE FROM schema_version")
    conn.execute(
        "INSERT INTO schema_version (version) VALUES (?)",
        (SCHEMA_VERSION,),
    )
    conn.commit()
    return conn


def _row_to_page(row: sqlite3.Row) -> LibraryCatalogPage:
    return LibraryCatalogPage(
        slug=row["slug"],
        title=row["title"],
        type=row["type"],
        source_pkg=row["source_pkg"],
        section=row["section"],
        updated=row["updated"],
        hash=row["hash"],
        derived_from=row["derived_from"],
    )


def upsert_page(conn: sqlite3.Connection, page: LibraryCatalogPage) -> None:
    """Insert or replace a single page. Commits the transaction on success.

    Matches the wiki-side ``lies.memory.catalog.upsert_page`` contract: the
    call is its own atomic unit, so the caller does not have to follow up
    with ``conn.commit()``. The single-row form is convenient for the
    per-doc catalog row written by ``LibraryWriter._upsert_catalog``;
    batch updates should prefer :func:`upsert_pages` for fewer fsyncs.
    """
    conn.execute(
        _UPSERT_SQL,
        (
            page.slug,
            page.title,
            page.type,
            page.source_pkg,
            page.section,
            page.updated,
            page.hash,
            page.derived_from,
        ),
    )
    conn.commit()


def upsert_pages(conn: sqlite3.Connection, pages: Iterable[LibraryCatalogPage]) -> None:
    """Insert or replace multiple pages in one transaction; commit on success.

    Matches the wiki-side ``lies.memory.catalog.upsert_pages`` contract.
    Empty input is a no-op (the early return avoids an empty ``executemany``
    call AND skips the ``commit()`` so a no-op update never forces a
    fsync).
    """
    pages = list(pages)
    if not pages:
        return
    conn.executemany(
        _UPSERT_SQL,
        [
            (p.slug, p.title, p.type, p.source_pkg, p.section, p.updated, p.hash, p.derived_from)
            for p in pages
        ],
    )
    conn.commit()


def remove_page(conn: sqlite3.Connection, slug: str) -> None:
    """Delete a page by slug. Commits the transaction on success.

    Matches the wiki-side ``lies.memory.catalog.remove_page`` contract: the
    call is its own atomic unit, so the caller does not need a follow-up
    ``conn.commit()``. (The wiki version returns ``bool`` for "did a row
    match"; the library version returns ``None`` because no current caller
    needs the bool. Kept simple.)
    """
    conn.execute("DELETE FROM pages WHERE slug = ?", (slug,))
    conn.commit()


def list_pages(
    conn: sqlite3.Connection,
    *,
    section: str | None = None,
    source_pkg: str | None = None,
) -> list[LibraryCatalogPage]:
    sql = "SELECT * FROM pages"
    clauses: list[str] = []
    params: list[str] = []
    if section is not None:
        clauses.append("section = ?")
        params.append(section)
    if source_pkg is not None:
        clauses.append("source_pkg = ?")
        params.append(source_pkg)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY slug"
    return [_row_to_page(r) for r in conn.execute(sql, params).fetchall()]


def list_slugs(conn: sqlite3.Connection) -> set[str]:
    return {r["slug"] for r in conn.execute("SELECT slug FROM pages")}


__all__ = (
    "SCHEMA_VERSION",
    "LibraryCatalogPage",
    "open_catalog",
    "upsert_page",
    "upsert_pages",
    "remove_page",
    "list_pages",
    "list_slugs",
)
