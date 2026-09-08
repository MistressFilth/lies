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
    typer.echo("done.")
