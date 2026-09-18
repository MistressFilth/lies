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

import json
import sqlite3
from collections.abc import Iterable
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
    ``orphan=True`` filters to rows where at least one cited slug does
    not resolve to any row in the catalog (i.e. catalog membership only
    — NOT disk existence). The ``dangling_derived_from`` lint in
    ``src/lies/orchestrator.py`` instead checks disk existence; the
    two definitions can diverge when the catalog is stale, when a
    cited slug points at a system file (``index.md`` / ``log.md`` /
    ``schema.md`` / ``overview.md`` / ``lint-report.md`` are excluded
    from the catalog walk), or after a ``reconcile`` that removed a
    stale row. Run ``lies catalog reconcile`` first if you need
    lint-style disk parity.

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


def render_provenance_tsv(records: Iterable[ProvenanceRecord]) -> str:
    """Render records as header-less TSV, one row per page.

    Columns: ``slug\ttitle\ttype\tsource_pkg\tupdated\tcsv(sources)``.
    The trailing comma-joined ``derived_from`` mirrors the catalog's
    storage shape and is awk/grep-friendly.

    The CLI rejects slugs containing ``\\t`` / ``\\n`` / ``\\r`` /
    ``\\x0b`` / ``\\x0c`` at validation time, so the renderer can
    safely ``\\t``-join raw fields without escaping. Round-tripping
    caller-supplied ``ProvenanceRecord`` objects directly (e.g. from
    tests or future MCP surfaces) does NOT enforce the same
    constraint — callers that bypass ``_validate_page_slug`` are
    responsible for sanitising their inputs.
    """
    lines: list[str] = []
    for rec in records:
        sources_csv = ",".join(rec.derived_from)
        lines.append(
            "\t".join(
                (
                    rec.slug,
                    rec.title,
                    rec.type,
                    rec.source_pkg,
                    rec.updated,
                    sources_csv,
                )
            )
        )
    return "\n".join(lines)


def render_provenance_json(records: Iterable[ProvenanceRecord]) -> str:
    """Render records as a JSON array of objects.

    Shape is a subset of ``lies catalog dump --json``: provenance
    surfaces only the columns a reader needs to trace a synthesis
    back to its sources (``slug``, ``title``, ``type``, ``source_pkg``,
    ``updated``, ``derived_from``); ``section`` and ``hash`` are
    omitted. ``derived_from`` is a JSON array (not the comma-joined
    storage string), mirroring the in-memory tuple.
    """
    payload = [
        {
            "slug": rec.slug,
            "title": rec.title,
            "type": rec.type,
            "source_pkg": rec.source_pkg,
            "updated": rec.updated,
            "derived_from": list(rec.derived_from),
        }
        for rec in records
    ]
    return json.dumps(payload, indent=2)


__all__ = (
    "ProvenanceRecord",
    "list_provenance_pages",
    "render_provenance_json",
    "render_provenance_tsv",
)
