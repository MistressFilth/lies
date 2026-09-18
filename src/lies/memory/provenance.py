"""Read-only provenance surface over the wiki catalog.

Walks the ``derived_from`` column added by F3 (PR #50) and surfaces
synthesised pages plus their cited slugs. Pure data layer: no typer,
no orchestrator, no ``Wiki`` import beyond the type stub needed by
``open_catalog``.

Used by ``src/lies/cli/wiki.py:provenance`` (F29). The MCP
``wiki_provenance`` resource is a planned sibling; this helper is
structured to support it without modification.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class ProvenanceRecord:
    """One synthesised page's provenance, ready for CLI/MCP output."""

    slug: str
    title: str
    type: str
    source_pkg: str
    updated: str
    derived_from: tuple[str, ...]


def list_provenance_pages(
    conn: sqlite3.Connection,
    *,
    page: str | None = None,
    orphan: bool = False,
) -> list[ProvenanceRecord]:
    """Return every page with non-empty ``derived_from``.

    ``page`` restricts to a single slug (0–1 records).
    ``orphan=True`` filters to rows where at least one cited slugs
    does not resolve to any row in ``conn`` (symmetric with the
    ``dangling_derived_from`` lint category at
    ``src/lies/orchestrator.py:336``).

    Catalog storage represents ``derived_from`` as a comma-joined
    string; this function splits + filters empty fragments so the
    returned tuple mirrors the on-disk YAML frontmatter shape.
    """
    rows = conn.execute(
        "SELECT slug, title, type, source_pkg, updated, derived_from "
        "FROM pages WHERE derived_from != ''"
    ).fetchall()
    all_slugs = {r["slug"] for r in conn.execute("SELECT slug FROM pages").fetchall()}
    records: list[ProvenanceRecord] = []
    for row in rows:
        if page is not None and row["slug"] != page:
            continue
        sources = tuple(s for s in row["derived_from"].split(",") if s)
        if orphan and all(s in all_slugs for s in sources):
            continue
        records.append(
            ProvenanceRecord(
                slug=row["slug"],
                title=row["title"] or "",
                type=row["type"] or "",
                source_pkg=row["source_pkg"] or "",
                updated=row["updated"] or "",
                derived_from=sources,
            )
        )
    return records


__all__ = ("ProvenanceRecord", "list_provenance_pages")
