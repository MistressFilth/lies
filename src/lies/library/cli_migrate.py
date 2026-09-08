"""``lies migrate ingest-to-library`` CLI."""

from __future__ import annotations

from typing import Annotated

import typer

from lies.cli import app
from lies.library.migrate import MigrationPlan, apply_migration, plan_migration
from lies.library.paths import Library
from lies.wiki.git import atomic_commit


@app.command(
    name="ingest-to-library",
    short_help="Move wiki-resident ingests into the library.",
    rich_help_panel="Migration",
)
def ingest_to_library(
    collection: Annotated[
        str | None,
        typer.Option(
            "--collection",
            help="Limit migration to one collection (default: all).",
        ),
    ] = None,
    name: Annotated[
        str | None,
        typer.Option(
            "--name",
            envvar="LIES_WIKI_NAME",
            help="Wiki to read from (default: $LIES_WIKI_NAME).",
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run/--apply",
            help="Dry-run (default: print plan, write nothing).",
        ),
    ] = True,
    date_str: Annotated[
        str,
        typer.Option(
            "--date",
            help="Backup directory name (default: today YYYY-MM-DD).",
        ),
    ] = "today",
) -> None:
    """One-shot migration script. See spec section 5."""
    from lies.cli import resolve_wiki as _resolve_wiki

    # Deferred import: ``lies.qmd.cli`` runs ``lies.qmd.__init__``, which
    # pulls in fastmcp + pydantic_ai (via ``lies.qmd.capability``).
    # Importing here keeps ``import lies.cli`` cheap; the
    # ``test_cli_lazy_imports`` no-fastmcp / no-pydantic_ai contract is
    # preserved. Same rationale as ``library/writer.py``.
    from lies.qmd import cli as _qmd_cli

    wiki = _resolve_wiki(name)
    lib = Library.open()
    plan = plan_migration(wiki, lib, date_str=date_str)
    if collection is not None:
        plan = MigrationPlan(
            moves=[
                (s, d) for s, d in plan.moves if s.relative_to(wiki.wiki_dir).parts[0] == collection
            ],
            duplicates_to_backup=plan.duplicates_to_backup,
            catalog_updates=plan.catalog_updates,
        )
    typer.echo(
        f"plan: {len(plan.moves)} pages move, {len(plan.duplicates_to_backup)} duplicates backup"
    )
    if dry_run:
        typer.echo("(dry-run; pass --apply to mutate)")
        return
    apply_migration(plan, lib, dry_run=False)
    sha = atomic_commit(
        wiki.data_root,
        f"migrate: ingest-to-library +{len(plan.moves)}",
    )
    if sha:
        typer.echo(f"wiki commit {sha[:8]}")
    # Post-apply qmd cleanup hook (Task 14): for each collection that
    # just moved to the library, unregister the per-wiki
    # ``<wiki>_<collection>`` qmd index and register the library-side
    # collection. The wiki-side atomic_commit above has already landed;
    # qmd is a derived index, so every failure here is non-fatal and the
    # migration commit stands.
    collections_moved = sorted({src.relative_to(wiki.wiki_dir).parts[0] for src, _ in plan.moves})
    for coll in collections_moved:
        try:
            _qmd_cli.qmd_collection_remove(wiki.data_root, f"{wiki.name}_{coll}")
        except Exception:  # noqa: BLE001 - qmd is derived; non-fatal
            pass
        try:
            _qmd_cli.qmd_collection_add_or_update(
                lib.git_root,
                lib.collections_root / coll,
                coll,
                library_target=lib.collections_root / coll,
            )
            _qmd_cli.qmd_update(lib.git_root)
            _qmd_cli.qmd_embed(lib.git_root, coll)
        except Exception:  # noqa: BLE001 - qmd is derived; non-fatal
            pass
    typer.echo("done.")
