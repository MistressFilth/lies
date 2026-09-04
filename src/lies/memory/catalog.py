"""SQLite-backed wiki page catalog.

Mirrors ``ask/repo/ask/scripts/catalog.py``: WAL mode + busy_timeout,
single-page or batch upsert, replace-on-conflict, and a CHECK constraint
on ``section``. Schema versioned in a ``schema_version`` table; this
PR ships v1 (initial schema). Future migrations are additive only.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path

from lies.memory.catalog_models import CatalogPage, PageSection


SCHEMA_VERSION = 1


_DDL = """\
CREATE TABLE IF NOT EXISTS pages (
    slug         TEXT PRIMARY KEY,
    title        TEXT NOT NULL DEFAULT '',
    type         TEXT NOT NULL DEFAULT '',
    source_pkg   TEXT NOT NULL DEFAULT '',
    section      TEXT NOT NULL DEFAULT 'wiki'
        CHECK(section IN ('wiki', 'ingested')),
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


def _catalog_path(wiki: object) -> Path:
    return wiki.wiki_dir / ".lies" / "catalog.db"  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]


def open_catalog(wiki: object) -> sqlite3.Connection:
    """Open (or create) ``<wiki_dir>/.lies/catalog.db``.

    Creates the schema, sets WAL journal mode and a 5-second busy
    timeout, and stamps the current ``SCHEMA_VERSION``. Idempotent.
    Caller owns the connection.
    """
    catalog_path = _catalog_path(wiki)
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


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def upsert_page(conn: sqlite3.Connection, page: CatalogPage) -> None:
    """Insert or replace a single page in the catalog."""
    conn.execute(
        _UPSERT_SQL,
        (
            page.slug,
            page.title,
            page.type,
            page.source_pkg,
            page.section.value,
            page.updated,
            page.hash,
            page.derived_from,
        ),
    )
    conn.commit()


def upsert_pages(conn: sqlite3.Connection, pages: list[CatalogPage]) -> None:
    """Insert or replace multiple pages in a single transaction."""
    if not pages:
        return
    conn.executemany(
        _UPSERT_SQL,
        [
            (
                p.slug,
                p.title,
                p.type,
                p.source_pkg,
                p.section.value,
                p.updated,
                p.hash,
                p.derived_from,
            )
            for p in pages
        ],
    )
    conn.commit()


def remove_page(conn: sqlite3.Connection, slug: str) -> bool:
    """Delete a page by slug. Returns True when a row was deleted."""
    cur = conn.execute("DELETE FROM pages WHERE slug = ?", (slug,))
    conn.commit()
    return cur.rowcount > 0


def remove_pages(conn: sqlite3.Connection, slugs: Iterable[str]) -> int:
    """Delete multiple pages. Returns count of rows deleted."""
    slug_list = list(slugs)
    if not slug_list:
        return 0
    placeholders = ",".join("?" * len(slug_list))
    cur = conn.execute(
        f"DELETE FROM pages WHERE slug IN ({placeholders})",
        slug_list,
    )
    conn.commit()
    return cur.rowcount


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def get_page(conn: sqlite3.Connection, slug: str) -> CatalogPage | None:
    row = conn.execute(
        "SELECT slug, title, type, source_pkg, section, updated, hash, derived_from "
        "FROM pages WHERE slug = ?",
        (slug,),
    ).fetchone()
    if row is None:
        return None
    return _row_to_page(row)


def list_pages(
    conn: sqlite3.Connection,
    *,
    section: str | PageSection | None = None,
    page_type: str | None = None,
    source_pkg: str | None = None,
) -> list[CatalogPage]:
    clauses: list[str] = []
    params: list[object] = []
    if section is not None:
        clauses.append("section = ?")
        params.append(section.value if isinstance(section, PageSection) else section)
    if page_type is not None:
        clauses.append("type = ?")
        params.append(page_type)
    if source_pkg is not None:
        clauses.append("source_pkg = ?")
        params.append(source_pkg)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        "SELECT slug, title, type, source_pkg, section, updated, hash, derived_from "
        f"FROM pages{where}",
        params,
    ).fetchall()
    return [_row_to_page(r) for r in rows]


def count_pages(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0])


def slug_exists(conn: sqlite3.Connection, slug: str) -> bool:
    return (
        conn.execute("SELECT 1 FROM pages WHERE slug = ? LIMIT 1", (slug,)).fetchone() is not None
    )


def list_slugs(conn: sqlite3.Connection) -> set[str]:
    return {r["slug"] for r in conn.execute("SELECT slug FROM pages").fetchall()}


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------


def _row_to_page(row: sqlite3.Row) -> CatalogPage:
    section_value = row["section"]
    section = PageSection(section_value) if section_value else PageSection.wiki
    return CatalogPage(
        slug=row["slug"],
        title=row["title"],
        type=row["type"],
        source_pkg=row["source_pkg"],
        section=section,
        updated=row["updated"],
        hash=row["hash"],
        derived_from=row["derived_from"],
    )
