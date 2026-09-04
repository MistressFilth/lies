"""SQLite-backed wiki page catalog.

Mirrors ``ask/repo/ask/scripts/catalog.py``: WAL mode + busy_timeout,
single-page or batch upsert, replace-on-conflict, and a CHECK constraint
on ``section``. Schema versioned in a ``schema_version`` table; this
PR ships v1 (initial schema). Future migrations are additive only.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
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


# ---------------------------------------------------------------------------
# Disk sync (orphan + dangling)
# ---------------------------------------------------------------------------

_SYSTEM_FILES = frozenset({"log.md", "schema.md", "index.md"})


@dataclass
class ReconcileResult:  # type: ignore[no-redef]
    """Result of a `reconcile(wiki)` call."""

    added: int = 0
    removed: int = 0
    would_add: int = 0
    would_remove: int = 0


def _iter_disk_slugs(wiki: object) -> set[str]:
    """Return wiki slugs for every markdown file under ``wiki.wiki_dir``.

    Slugs are derived via ``CatalogPage.from_path`` so the result matches
    the catalog's stored slug form (no leading ``wiki/`` prefix, no
    ``.md`` suffix). Skips system files (``log.md``, ``schema.md``,
    ``index.md``) and catalog sibling files (``catalog.db*``).
    """
    wiki_dir: Path = wiki.wiki_dir  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]
    slugs: set[str] = set()
    for md in wiki_dir.rglob("*.md"):
        if md.name in _SYSTEM_FILES:
            continue
        if md.name.startswith("catalog.db"):
            continue
        rel = md.relative_to(wiki_dir).with_suffix("").as_posix()
        slugs.add(CatalogPage.from_path(wiki, rel).slug)
    return slugs


def rebuild_from_disk(wiki: object) -> list[CatalogPage]:
    """Walk ``<wiki_dir>/**/*.md`` and return one ``CatalogPage`` per file.

    Skips system files (``log.md`` / ``schema.md`` / ``index.md``) and the
    catalog's own sibling files (``catalog.db*``). Idempotent on second call.
    """
    wiki_dir: Path = wiki.wiki_dir  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]
    out: list[CatalogPage] = []
    for md in sorted(wiki_dir.rglob("*.md")):
        if md.name in _SYSTEM_FILES:
            continue
        if md.name.startswith("catalog.db"):
            continue
        rel = md.relative_to(wiki_dir).with_suffix("").as_posix()
        out.append(CatalogPage.from_path(wiki, rel))
    return out


def reconcile(wiki: object, *, dry_run: bool = False) -> ReconcileResult:
    """Reconcile catalog.db with files on disk.

    Two passes:
      1. Orphan pass: files on disk with no catalog row → upsert.
      2. Dangling pass: catalog rows with no disk file → remove.

    Returns a ``ReconcileResult`` whose ``added`` / ``removed`` count the
    applied rows, or whose ``would_add`` / ``would_remove`` count the
    would-be changes when ``dry_run=True``.
    """
    conn = open_catalog(wiki)
    try:
        indexed = list_slugs(conn)
    finally:
        conn.close()

    disk_slugs = _iter_disk_slugs(wiki)

    orphans = sorted(disk_slugs - indexed)
    dangling = sorted(indexed - disk_slugs)

    if dry_run:
        return ReconcileResult(
            would_add=len(orphans),
            would_remove=len(dangling),
        )

    conn = open_catalog(wiki)
    try:
        if orphans:
            pages = rebuild_from_disk(wiki)
            keep = [p for p in pages if p.slug in set(orphans)]
            upsert_pages(conn, keep)
        if dangling:
            remove_pages(conn, dangling)
    finally:
        conn.close()
    return ReconcileResult(added=len(orphans), removed=len(dangling))


def render_markdown(conn: sqlite3.Connection) -> str:
    """Title-only markdown export. One ``- [Title](path)`` per row.

    Closes P2 ``wiki/index.md collapses to title-only``: the current
    title-only behavior becomes canonical.
    """
    pages = list_pages(conn, section=PageSection.wiki)
    pages.sort(key=lambda p: (p.source_pkg, p.slug))
    lines = ["# Wiki Catalog", ""]
    for p in pages:
        lines.append(f"- [{p.title}](wiki/{p.source_pkg}/{p.slug}.md)")
    return "\n".join(lines) + "\n"
