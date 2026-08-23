"""Source ingestion panel: ingest, ingest_source, sync, reindex.

Orchestrator + heavy ETL imports stay inside each command body so
``import lies.cli`` doesn't pay for the model stack.
"""

from __future__ import annotations

from typing import Annotated

import typer

from lies.cli import app
from lies.cli._helpers import configure_logging

__all__ = (
    "ingest",
    "ingest_source",
    "reindex",
    "sync",
)


@app.command(
    short_help="Ingest a source into a collection (creates collection if missing).",
    rich_help_panel="Source ingestion",
)
def ingest(
    collection: str,
    *,
    source: Annotated[
        str | None,
        typer.Option(
            "--source",
            help="Path, URL, or '-' for stdin (default: prompt).",
        ),
    ] = None,
    model: Annotated[
        str | None,
        typer.Option(
            "--model",
            help="Override the model id used for ingestion (default: project's default_model).",
        ),
    ] = None,
    name: str | None = typer.Option(
        None,
        "--name",
        envvar="LIES_WIKI_NAME",
        help="Wiki to ingest into (default: $LIES_WIKI_NAME).",
    ),
) -> None:
    """Ingest a source into a collection (creates collection if missing).

    First-time flow (LLM scraper generation) deferred to follow-up.
    Currently behaves like sync for existing collections.

    Errors if the collection YAML is missing; ``sync_helper.sync_collection``
    does not auto-scaffold a collection from a source path. Use
    ``ingest_source`` for the legacy single-source ingestion path.
    """
    from lies.cli import resolve_wiki
    from lies.etl.sync_helper import (
        acquire_heartbeat,
        release_heartbeat,
        sync_collection,
    )

    wiki = resolve_wiki(name)
    if acquire_heartbeat(wiki, wait=False, fail_busy=True) is None:
        raise typer.Exit(code=2)
    try:
        sync_collection(wiki, collection, force=False)
    finally:
        release_heartbeat(wiki)


@app.command(
    short_help="Atomic ingest of a single source into the wiki identified by --name.",
    rich_help_panel="Source ingestion",
)
def ingest_source(
    source: str = typer.Argument(..., help="Path, URL, or '-' for stdin."),
    name: str | None = typer.Option(
        None,
        "--name",
        envvar="LIES_WIKI_NAME",
        help="Wiki to ingest into (default: $LIES_WIKI_NAME).",
    ),
) -> None:
    """Atomic ingest of a single source into the wiki identified by ``name``.

    Kept for backward compatibility with the original source-path CLI
    surface (``lies ingest <source>``). Delegates to
    :meth:`Orchestrator.run_ingest`, which snapshots the working
    tree, runs the agent, and commits atomically. On any failure the
    working tree is restored and the exception is re-raised.
    """
    from lies.cli import Orchestrator, resolve_wiki

    configure_logging()
    wiki = resolve_wiki(name)
    orch = Orchestrator(wiki)
    output = orch.run_ingest(source)
    typer.echo(output)


@app.command(
    short_help="Sync one or all collections.",
    rich_help_panel="Source ingestion",
)
def sync(
    collection: Annotated[
        str | None,
        typer.Argument(help="Collection to sync (omit to sync every collection in the wiki)."),
    ] = None,
    *,
    force: Annotated[
        bool,
        typer.Option(
            "--force/--no-force",
            help="Force-sync even if a sync is in progress (default: fail-busy).",
        ),
    ] = False,
    wait: Annotated[
        bool,
        typer.Option(
            "--wait/--no-wait",
            help="Wait for an in-progress sync to finish before proceeding (default: exit immediately).",
        ),
    ] = False,
    fail_busy: Annotated[
        bool,
        typer.Option(
            "--fail-busy/--no-fail-busy",
            help="Return non-zero exit code if a sync is in progress (default: wait).",
        ),
    ] = False,
    name: str | None = typer.Option(
        None, "--name", envvar="LIES_WIKI_NAME", help="Wiki to sync (default: $LIES_WIKI_NAME)."
    ),
) -> None:
    """Sync one or all collections."""
    from lies.cli import resolve_wiki
    from lies.etl.sync_helper import (
        acquire_heartbeat,
        collection_names,
        release_heartbeat,
        sync_collection,
    )

    wiki = resolve_wiki(name)
    if acquire_heartbeat(wiki, wait=wait, fail_busy=fail_busy) is None:
        raise typer.Exit(code=2)
    try:
        for coll_name in collection_names(wiki, collection):
            sync_collection(wiki, coll_name, force=force)
    finally:
        release_heartbeat(wiki)


@app.command(
    short_help="Reindex QMD collections.",
    rich_help_panel="Source ingestion",
)
def reindex(
    *,
    reconcile: Annotated[
        bool,
        typer.Option(
            "--reconcile/--no-reconcile",
            help="Reconcile the qmd index with the wiki's collection directory before reindexing (default: just reindex).",
        ),
    ] = False,
    name: str | None = typer.Option(
        None, "--name", envvar="LIES_WIKI_NAME", help="Wiki to reindex (default: $LIES_WIKI_NAME)."
    ),
) -> None:
    """Reindex QMD collections.

    ``--reconcile`` syncs each collection (running the full pipeline) and
    rebuilds the in-memory wikilink corpus for downstream consumers.
    """
    from lies.cli import WikiLinkResolver, resolve_wiki
    from lies.etl.sync_helper import collection_names, sync_collection

    wiki = resolve_wiki(name)
    if reconcile:
        for coll_name in collection_names(wiki, None):
            sync_collection(wiki, coll_name, force=False)
        # Spec: reindex rebuilds the corpus. No in-process consumer today
        # (YAGNI); held for the lifetime of this process.
        WikiLinkResolver.build((wiki.wiki_dir, wiki.raw_dir))
