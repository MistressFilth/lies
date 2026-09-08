"""Deprecated Phase-1 ingest entry points; one minor version of grace.

The deterministic ingest lives at ``lies ingest`` (``lies.library.cli``).
The LLM-distilled Phase-1 paths are removed; ``ingest-source`` is kept
only as a deprecated stub that emits an error directing operators to
the new subcommand. The ``--no-llm`` opt-out is gone with it.

``sync`` and ``reindex`` remain in place — they are wiki-side
collection / QMD commands, not ingest paths, and continue to operate
unchanged.
"""

from __future__ import annotations

import typer

from lies.cli import app

__all__ = ("ingest_source_stub",)


@app.command(
    name="ingest-source",
    short_help="[deprecated] Use 'lies ingest --source' instead.",
    deprecated=True,
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def ingest_source_stub() -> None:
    """Deprecated. Use ``lies ingest --source <path> --collection NAME``."""
    typer.echo(
        "error: 'lies ingest-source' is removed. Use 'lies ingest --source'.",
        err=True,
    )
    raise typer.Exit(code=2)


# ---------------------------------------------------------------------------
# Wiki-side: ``sync`` and ``reindex`` are unchanged from their Phase-1
# implementations. They live in this module because they share the
# ``Source ingestion`` rich-help panel with the legacy ``ingest`` /
# ``ingest-source`` commands they used to flank. Removing the legacy
# commands and keeping these keeps the help-panel grouping intact.
# ---------------------------------------------------------------------------


def __getattr__(name: str):
    """Lazy re-export so tests can ``mock.patch`` ``lies.cli.ingestion.Orchestrator``.

    Same PEP-562 pattern as the Phase-1 version: ``mock.patch`` of
    ``lies.cli.ingestion.Orchestrator`` lets tests intercept the
    Orchestrator factory the sync/reindex bodies still pull through.
    """
    if name == "Orchestrator":
        from lies.cli import Orchestrator as _OrchestratorCls

        globals()[name] = _OrchestratorCls
        return _OrchestratorCls
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals().keys()) | {"Orchestrator"})


from typing import Annotated  # noqa: E402


@app.command(
    short_help="Sync one or all collections (single-collection mode bootstraps from --source).",
    rich_help_panel="Source ingestion",
)
def sync(
    collection: Annotated[
        str | None,
        typer.Argument(help="Collection to sync (omit to sync every collection in the wiki)."),
    ] = None,
    *,
    source: Annotated[
        str | None,
        typer.Option(
            "--source",
            help="Bootstrap a missing collection from this source (single-collection mode only).",
        ),
    ] = None,
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
    wizard: Annotated[
        bool,
        typer.Option(
            "--wizard",
            help="Route through collection_author_agent for missing collections (requires TTY).",
        ),
    ] = False,
) -> None:
    """Sync one or all collections.

    In single-collection mode (positional arg given), pass ``--source`` to
    bootstrap a missing collection before syncing. With ``--wizard`` and a
    TTY, the bootstrap routes through the collection_author_agent.

    Multi-collection mode (no positional) only iterates existing YAMLs.
    """
    from lies.collections.bootstrap import bootstrap_collection, ensure_wiki
    from lies.collections.errors import (
        CollectionMismatch,
        WikiLayoutInitFailed,
        WizardRequiresTTY,
    )
    from lies.config import get_wiki_name
    from lies.etl.sync_helper import (
        acquire_heartbeat,
        collection_names,
        release_heartbeat,
        sync_collection,
    )

    try:
        wiki = ensure_wiki(name or get_wiki_name())
    except WikiLayoutInitFailed as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=5)
    if acquire_heartbeat(wiki, wait=wait, fail_busy=fail_busy) is None:
        raise typer.Exit(code=2)
    total_errors = 0
    total_created = 0
    total_updated = 0
    total_skipped = 0
    try:
        if collection is not None and source is not None:
            try:
                bootstrap_collection(wiki, collection, source, wizard=wizard)
            except WizardRequiresTTY:
                typer.echo(
                    "error: --wizard needs a TTY; run interactively "
                    "or omit --wizard for bare scaffold",
                    err=True,
                )
                raise typer.Exit(code=4)
            except CollectionMismatch as exc:
                typer.echo(
                    f"error: collection {collection!r} exists with source "
                    f"{exc.existing_source!r}; requested {exc.requested_source!r}. "
                    f"Use `lies collections modify --set source=...` to change.",
                    err=True,
                )
                raise typer.Exit(code=3)
        last_result = None
        for coll_name in collection_names(wiki, collection):
            last_result = sync_collection(wiki, coll_name, force=force)
            total_errors += last_result.errors
            total_created += last_result.created
            total_updated += last_result.updated
            total_skipped += last_result.skipped
        summary = (
            f"created={total_created} updated={total_updated} "
            f"skipped={total_skipped} errors={total_errors}"
        )
        typer.echo(summary)
        if total_errors:
            raise typer.Exit(code=1)
        # Suppress unused-binding: ``last_result`` is referenced for
        # the single-collection fast path below if we ever inline.
        del last_result
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


__all__ = (
    "ingest_source_stub",
    "sync",
    "reindex",
)
