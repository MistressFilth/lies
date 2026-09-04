"""``lies catalog`` CLI group.

Five subcommands for inspecting + maintaining the wiki's sqlite
catalog (``catalog.db``):

- ``status`` — row count + schema version.
- ``dump`` — list every row (or filtered subset).
- ``reconcile`` — sync ``catalog.db`` with files on disk.
- ``rebuild`` — force a full backfill from disk.
- ``render`` — emit the title-only markdown derivative.

Each command takes ``--name NAME`` (env: ``LIES_WIKI_NAME``) and
resolves the wiki via ``lies.cli.resolve_wiki`` — the same shim
``lies cli memory`` and ``lies cli status`` use. The catalog module
only reads ``wiki.wiki_dir`` from the resolved wiki, so the same
``Wiki(...)`` instance works for both CLI and library callers.

The read commands (``status`` / ``dump`` / ``render``) and
``reconcile`` lazily backfill the catalog from disk on first open
via :func:`_open_catalog_with_seed`. Without that seed, a fresh
wiki's ``catalog.db`` is empty even though there are pages on disk
(the etl WRITE stage and the WikiMemoryService normally populate
it, but a CLI user poking around with raw files should not have to
manually ``rebuild`` first to see the catalog's contents).
``rebuild`` is the explicit seed command and does not auto-seed.

Heavy ``lies.memory.catalog`` imports stay inside the command
bodies: the catalog itself is pure sqlite + filesystem walks, no
orchestrator / pydantic_ai / anthropic dependency.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from lies.wiki.wiki import Wiki

__all__ = ("catalog_app",)


catalog_app = typer.Typer(
    name="catalog",
    help="Wiki page catalog (sqlite).",
    rich_help_panel="Querying and maintenance",
    no_args_is_help=True,
)


def _resolve_wiki(name: str | None = None) -> Wiki:
    """Resolve the active wiki via ``lies.cli.resolve_wiki``.

    Mirrors the shim in ``lies.cli.memory`` — importing the heavy
    orchestrator / pydantic_ai / fastmcp stack is deferred by the
    PEP 562 ``__getattr__`` in ``cli/__init__.py``.
    """
    from lies.cli import resolve_wiki

    return resolve_wiki(name)


def _open_catalog_with_seed(wiki: Wiki):
    """Open the catalog, backfilling from disk if empty.

    The catalog is normally populated by ``WikiMemoryService`` per-op
    upserts during memory operations, and by the etl WRITE stage on
    ingest. A bare ``catalog.db`` is therefore expected to mirror the
    wiki's ``.md`` files. When a CLI user pokes around without going
    through those paths, the catalog can be empty even though pages
    exist on disk — this helper seeds the empty case from
    ``rebuild_from_disk`` so the read commands reflect reality.
    """
    from lies.memory.catalog import (
        count_pages,
        open_catalog,
        rebuild_from_disk,
        upsert_pages,
    )

    conn = open_catalog(wiki)
    if count_pages(conn) == 0:
        pages = rebuild_from_disk(wiki)
        if pages:
            upsert_pages(conn, pages)
    return conn


@catalog_app.command("status")
def status_cmd(
    name: Annotated[
        str | None,
        typer.Option(
            "--name",
            envvar="LIES_WIKI_NAME",
            help="Wiki to inspect (default: $LIES_WIKI_NAME).",
        ),
    ] = None,
) -> None:
    """Print row count + schema version."""
    from lies.memory.catalog import count_pages

    wiki = _resolve_wiki(name)
    conn = _open_catalog_with_seed(wiki)
    try:
        n = count_pages(conn)
        row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
    finally:
        conn.close()
    schema_ver = row[0] if row else "unknown"
    typer.echo(f"catalog.db: {n} pages, schema v{schema_ver}")


@catalog_app.command("dump")
def dump_cmd(
    name: Annotated[
        str | None,
        typer.Option(
            "--name",
            envvar="LIES_WIKI_NAME",
            help="Wiki to inspect (default: $LIES_WIKI_NAME).",
        ),
    ] = None,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Emit a JSON array of full row dicts."),
    ] = False,
    source_pkg: Annotated[
        str | None,
        typer.Option("--source-pkg", help="Filter by source_pkg."),
    ] = None,
    type_filter: Annotated[
        str | None,
        typer.Option("--type", help="Filter by page type."),
    ] = None,
) -> None:
    """Dump all rows (optionally filtered)."""
    from lies.memory.catalog import list_pages

    wiki = _resolve_wiki(name)
    conn = _open_catalog_with_seed(wiki)
    try:
        pages = list_pages(
            conn,
            source_pkg=source_pkg,
            page_type=type_filter,
        )
    finally:
        conn.close()

    if as_json:
        typer.echo(json.dumps([p.model_dump(mode="json") for p in pages], indent=2))
        return
    for p in pages:
        typer.echo(f"{p.slug}\t{p.title}\t{p.type}\t{p.source_pkg}\t{p.updated}")


@catalog_app.command("reconcile")
def reconcile_cmd(
    name: Annotated[
        str | None,
        typer.Option(
            "--name",
            envvar="LIES_WIKI_NAME",
            help="Wiki to inspect (default: $LIES_WIKI_NAME).",
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Report would-be adds/removes without writing.",
        ),
    ] = False,
) -> None:
    """Sync catalog.db with files on disk (orphan + dangling pass).

    Seeds the catalog from disk first so the orphan / dangling sets
    reflect on-disk truth rather than the empty initial state.
    """
    from lies.memory.catalog import reconcile

    wiki = _resolve_wiki(name)
    # Force-seed before reconciling so reconcile operates on a
    # populated catalog, not an empty one (which would treat every
    # disk file as an orphan).
    _open_catalog_with_seed(wiki).close()
    result = reconcile(wiki, dry_run=dry_run)

    if dry_run:
        typer.echo(f"Would add {result.would_add}, Would remove {result.would_remove}")
        return
    typer.echo(f"Added {result.added}, removed {result.removed}")


@catalog_app.command("rebuild")
def rebuild_cmd(
    name: Annotated[
        str | None,
        typer.Option(
            "--name",
            envvar="LIES_WIKI_NAME",
            help="Wiki to inspect (default: $LIES_WIKI_NAME).",
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Report would-be upserts without writing.",
        ),
    ] = False,
) -> None:
    """Force a full backfill from disk (same as first-open behavior)."""
    from lies.memory.catalog import open_catalog, rebuild_from_disk, upsert_pages

    wiki = _resolve_wiki(name)
    pages = rebuild_from_disk(wiki)

    if dry_run:
        typer.echo(f"Would upsert {len(pages)} page(s)")
        return

    conn = open_catalog(wiki)
    try:
        upsert_pages(conn, pages)
    finally:
        conn.close()
    typer.echo(f"Upserted {len(pages)} page(s)")


@catalog_app.command("render")
def render_cmd(
    name: Annotated[
        str | None,
        typer.Option(
            "--name",
            envvar="LIES_WIKI_NAME",
            help="Wiki to inspect (default: $LIES_WIKI_NAME).",
        ),
    ] = None,
    out: Annotated[
        str | None,
        typer.Option(
            "--out",
            help="Output file (default: stdout).",
        ),
    ] = None,
) -> None:
    """Render markdown derivative (title-only)."""
    from lies.memory.catalog import render_markdown

    wiki = _resolve_wiki(name)
    conn = _open_catalog_with_seed(wiki)
    try:
        md = render_markdown(conn)
    finally:
        conn.close()

    if out:
        Path(out).write_text(md, encoding="utf-8")
        typer.echo(f"Wrote {out}")
        return
    typer.echo(md, nl=False)
