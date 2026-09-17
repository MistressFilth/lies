"""Wiki-side collection sync and qmd reindex commands.

``sync`` and ``reindex`` operate on the wiki's collection / QMD
surface, not the library ingest path. The deterministic library
ingest lives at ``lies ingest`` (``lies.library.cli``).
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, cast

import typer

from lies.cli import app

__all__ = ("sync", "reindex")


# ---------------------------------------------------------------------------
# Lazy re-export so tests can ``mock.patch`` ``lies.cli.ingestion.Orchestrator``.
#
# Same PEP-562 pattern as the established ``lies.cli/__init__.py`` shape:
# ``mock.patch`` of ``lies.cli.ingestion.Orchestrator`` lets tests intercept
# the Orchestrator factory the sync/reindex bodies still pull through.
# ---------------------------------------------------------------------------


def __getattr__(name: str):
    """Lazy re-export of ``Orchestrator`` for ``mock.patch``."""
    if name == "Orchestrator":
        from lies.cli import Orchestrator as _OrchestratorCls

        globals()[name] = _OrchestratorCls
        return _OrchestratorCls
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals().keys()) | {"Orchestrator"})


def _ensure_wiki(name: str):
    """Resolve ``name`` to a Wiki, auto-initializing it if missing.

    Local mirror of the legacy wiki-bootstrap helper. The library
    cutover dropped the wiki-scoped YAML config bootstrap module that
    previously owned this function; the body is the same one-liner
    wrapper over :func:`lies.cli._core._init_wiki_internal`.
    """
    from lies import xdg
    from lies.cli import resolve_wiki
    from lies.cli._core import _init_wiki_internal
    from lies.constants import LIES_DATA_SUBDIR
    from lies.errors import WikiNotRegistered
    from lies.wiki.wiki import Wiki

    try:
        return resolve_wiki(name)
    except WikiNotRegistered:
        pass
    wiki = Wiki(
        name=name,
        data_root=Wiki.data_root_for(name),
        config_root=xdg.config_home() / LIES_DATA_SUBDIR / name,
        cache_root=xdg.cache_home() / LIES_DATA_SUBDIR / name,
        state_root=xdg.state_home() / LIES_DATA_SUBDIR / name,
        runtime_root=xdg.runtime_dir_for(name),
    )
    try:
        _init_wiki_internal(wiki)
    except Exception as exc:
        raise _WikiLayoutInitFailed(name, exc) from exc
    return resolve_wiki(name)


class _WikiLayoutInitFailed(Exception):
    """WikiLayout.init() raised during auto-init (private to this module)."""

    def __init__(self, wiki_name: str, cause: BaseException) -> None:
        super().__init__(f"failed to auto-init wiki {wiki_name!r}: {cause}")
        self.wiki_name = wiki_name
        self.__cause__ = cause


class _WikiCollectionMismatch(Exception):
    """Existing wiki-yaml collection has a different source than requested.

    Private to this module; mirrors the public ``CollectionMismatch``
    contract from the legacy wiki-yaml bootstrap helper (with the same
    ``existing_source`` / ``requested_source`` attributes the sync
    command prints to stderr on collision).
    """

    def __init__(self, existing_source: str, requested_source: str) -> None:
        super().__init__(
            f"collection source mismatch: existing={existing_source!r}, "
            f"requested={requested_source!r}"
        )
        self.existing_source = existing_source
        self.requested_source = requested_source


class _WizardRequiresTTY(Exception):
    """--wizard requires an interactive TTY (private to this module)."""


def _bootstrap_wiki_collection(
    wiki: Any,
    name: str,
    source: str,
    *,
    wizard: bool = False,
) -> None:
    """Wiki-scoped bootstrap helper for ``lies sync --source``.

    Idempotently ensures a wiki-yaml collection config exists for
    ``(wiki, name)``. Mirrors the legacy wiki-yaml bootstrap contract so
    ``lies sync <name> --source <url>`` keeps writing
    ``wiki.collections_dir/<name>.yaml`` for callers that still rely on
    the wiki-yaml surface.

    - YAML exists + ``source`` matches → return.
    - YAML exists + ``source`` differs → raise :class:`_WikiCollectionMismatch`.
    - YAML missing + ``wizard=False`` → write a minimal record.
    - YAML missing + ``wizard=True`` → drive the
      ``collection_author_agent`` interactively.
    """

    if wizard and not sys.stdin.isatty():
        raise _WizardRequiresTTY()

    cfg_path = wiki.collections_dir / f"{name}.yaml"
    if cfg_path.exists():
        from lies.cli.collections import _load_collection

        existing = _load_collection(wiki, name)
        existing_source = (existing.source or "").strip()
        if existing_source and existing_source != source:
            raise _WikiCollectionMismatch(
                existing_source=existing.source,
                requested_source=source,
            )
        return

    if wizard:
        _bootstrap_via_wizard(wiki, name, source)
        return
    _bootstrap_bare(wiki, name, source)


def _bootstrap_bare(wiki: Any, name: str, source: str) -> None:
    """Write a minimal wiki-yaml record for ``(wiki, name)``."""
    from lies.cli.collections import _Collection, _save_collection

    now = datetime.now(tz=UTC)
    new = _Collection(
        name=name,
        path=wiki.data_root / "raw" / name,
        source=source,
        tags=[],
        scraper_cmd=None,
        doc_path=None,
        mapper_model=None,
        language=None,
        version="1",
        created_at=now,
        updated_at=now,
        config={},
    )
    _save_collection(wiki, new)


def _bootstrap_via_wizard(wiki: Any, name: str, source: str) -> None:
    """Drive :func:`collection_author_agent` interactively, then save."""
    from rich.prompt import Prompt

    from lies.agents.collection_author import (
        AuthorProposal,
        AuthorQuestion,
        CollectionAuthorDeps,
        collection_author_agent,
    )
    from lies.cli.collections import _Collection, _save_collection

    prompt = (
        f"{source} — describe how to ingest this corpus. Use tags to mark "
        f"sections; set scraper_cmd only if the default scraper is wrong."
    )
    agent = collection_author_agent()
    history: list[object] = []
    deps = CollectionAuthorDeps(manifest=[])
    while True:
        result = agent.run_sync(
            prompt,
            deps=deps,
            message_history=cast(Any, history),
        )
        history.append(result.new_messages())
        out = result.output
        if isinstance(out, AuthorQuestion):
            if out.options:
                answer = Prompt.ask(
                    out.prompt,
                    choices=out.options,
                    default=out.default or out.options[0],
                )
            elif out.default is not None:
                answer = Prompt.ask(out.prompt, default=out.default)
            else:
                answer = Prompt.ask(out.prompt)
            history.append({"role": "user", "content": f"{out.id}: {answer}"})
            continue
        if isinstance(out, AuthorProposal):
            now = datetime.now(tz=UTC)
            payload = dict(out.collection)
            payload.setdefault("name", name)
            payload.setdefault("path", str(wiki.data_root / "raw" / name))
            payload.setdefault("created_at", now)
            payload.setdefault("updated_at", now)
            for key in ("created_at", "updated_at"):
                if isinstance(payload.get(key), str):
                    payload[key] = datetime.fromisoformat(cast(str, payload[key]))
            payload["path"] = Path(payload["path"])
            doc_path = payload.get("doc_path")
            if doc_path is not None:
                payload["doc_path"] = Path(doc_path)
            coll = _Collection(**payload)
            _save_collection(wiki, coll)
            return
        # Any other agent output aborts the wizard.
        return


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
    from lies.config import get_wiki_name
    from lies.etl.sync_helper import (
        acquire_heartbeat,
        collection_names,
        release_heartbeat,
        sync_collection,
    )

    try:
        wiki = _ensure_wiki(name or get_wiki_name())
    except _WikiLayoutInitFailed as exc:
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
                _bootstrap_wiki_collection(wiki, collection, source, wizard=wizard)
            except _WizardRequiresTTY:
                typer.echo(
                    "error: --wizard needs a TTY; run interactively "
                    "or omit --wizard for bare scaffold",
                    err=True,
                )
                raise typer.Exit(code=4)
            except _WikiCollectionMismatch as exc:
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
