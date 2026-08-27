"""Source ingestion panel: ingest, ingest_source, sync, reindex.

Orchestrator + heavy ETL imports stay inside each command body so
``import lies.cli`` doesn't pay for the model stack.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

import typer

from lies.cli import app
from lies.cli._helpers import configure_logging

if TYPE_CHECKING:
    # Type-checker-only import for the lazy ``__getattr__`` re-export below.
    # The runtime import lives in ``__getattr__`` so ``import lies.cli``
    # does not pull in the orchestrator + pydantic_ai stack. ``noqa: TC004``
    # suppresses the "move out of TYPE_CHECKING" suggestion because the
    # runtime resolution is deliberate (lazy proxy).
    from lies.orchestrator import Orchestrator  # noqa: TC004

__all__ = (
    "ingest",
    "ingest_source",
    "reindex",
    "sync",
)


def __getattr__(name: str):
    """Lazy re-export of ``Orchestrator`` so tests can ``mock.patch`` it.

    Without this shim, ``mock.patch("lies.cli.ingestion.Orchestrator", ...)``
    cannot intercept a function-local ``from lies.cli import Orchestrator``
    (Python binds the import as a fast-local in the function frame, so the
    consumer module's namespace never sees the patched value). Routing the
    lookup through a module-level ``__getattr__`` lets ``ingest_source``
    reference ``Orchestrator`` as a bare name (PEP 562 module attribute
    lookup) while preserving the lazy-imports contract pinned by
    ``tests/unit/cli/test_cli_lazy_imports.py`` — orchestrator, pydantic_ai,
    and the anthropic SDK only load on first call, never on
    ``import lies.cli``. Same pattern as ``lies.cli.memory.sidecar``.
    """
    if name == "Orchestrator":
        from lies.cli import Orchestrator as _OrchestratorCls

        globals()[name] = _OrchestratorCls
        return _OrchestratorCls
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Include the lazy ``Orchestrator`` attr in ``dir()`` output."""
    return sorted(set(globals().keys()) | {"Orchestrator"})


@app.command(
    short_help="Bootstrap collection + wiki if missing.",
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
    wizard: Annotated[
        bool,
        typer.Option(
            "--wizard",
            help="Route through collection_author_agent for missing collections (requires TTY).",
        ),
    ] = False,
) -> None:
    """Ingest a source into a collection (creates collection if missing).

    First-time flow (LLM scraper generation) deferred to follow-up.
    Currently behaves like sync for existing collections.

    Errors if the collection YAML is missing; ``sync_helper.sync_collection``
    does not auto-scaffold a collection from a source path. Use
    ``ingest_source`` for the legacy single-source ingestion path.

    ``--wizard`` routes a missing collection through the interactive
    ``collection_author_agent`` so the operator can author tags, scraper
    command, etc. before any sync runs. Without a TTY the command exits 4.
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
        release_heartbeat,
        sync_collection,
    )

    try:
        wiki = ensure_wiki(name or get_wiki_name())
    except WikiLayoutInitFailed as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=5)
    if acquire_heartbeat(wiki, wait=False, fail_busy=True) is None:
        raise typer.Exit(code=2)
    try:
        try:
            bootstrap_collection(wiki, collection, source or "", wizard=wizard)
        except WizardRequiresTTY:
            typer.echo(
                "error: --wizard needs a TTY; run interactively or omit --wizard for bare scaffold",
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
        sync_collection(wiki, collection, force=False)
    finally:
        release_heartbeat(wiki)


@app.command(
    short_help="Atomic ingest of a single source into a collection (creates YAML if missing).",
    rich_help_panel="Source ingestion",
)
def ingest_source(
    source: str = typer.Argument(..., help="Path, URL, or '-' for stdin."),
    *,
    collection: str = typer.Option(
        ...,
        "--collection",
        help="Collection name to register the source under (writes a YAML if missing).",
    ),
    name: str | None = typer.Option(
        None,
        "--name",
        envvar="LIES_WIKI_NAME",
        help="Wiki to ingest into (default: $LIES_WIKI_NAME).",
    ),
    wizard: Annotated[
        bool,
        typer.Option(
            "--wizard",
            help="Route through collection_author_agent for missing collections (requires TTY).",
        ),
    ] = False,
) -> None:
    """Atomic ingest of a single source into the wiki identified by ``name``.

    Registers ``--collection`` (writes a minimal YAML if missing; refuses
    on source mismatch), then delegates to :meth:`Orchestrator.run_ingest`,
    which snapshots the working tree, runs the agent, and commits
    atomically. On any failure the working tree is restored and the
    exception is re-raised.

    ``--collection`` is required: the legacy URL-only ``lies ingest-source
    URL`` form is rejected up front (Typer missing-required-option).
    ``--wizard`` routes the registration step through
    ``collection_author_agent``; without a TTY the command exits 4.
    """
    from lies.collections.bootstrap import bootstrap_collection, ensure_wiki
    from lies.collections.errors import (
        CollectionMismatch,
        WikiLayoutInitFailed,
        WizardRequiresTTY,
    )
    from lies.config import get_wiki_name

    configure_logging()
    try:
        wiki = ensure_wiki(name if name is not None else get_wiki_name())
    except WikiLayoutInitFailed as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=5)
    try:
        bootstrap_collection(wiki, collection, source, wizard=wizard)
    except WizardRequiresTTY:
        typer.echo(
            "error: --wizard needs a TTY; run interactively or omit --wizard for bare scaffold",
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
    # ``Orchestrator`` is referenced as a bare name on purpose: the
    # module-level ``__getattr__`` above lazy-loads it on first call and
    # lets ``mock.patch("lies.cli.ingestion.Orchestrator", ...)`` intercept
    # it in tests without paying for the orchestrator import at CLI
    # startup.
    orch = Orchestrator(wiki)
    output = orch.run_ingest(source)
    typer.echo(output)


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
    """Sync one or all collections (bootstraps single-collection mode if --source).

    ``--wizard`` routes the bootstrap step through
    ``collection_author_agent`` when a single-collection ``--source`` is
    supplied and the YAML does not yet exist. Without a TTY the command
    exits 4.
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
