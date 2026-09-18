"""``lies wiki`` — read views over the wiki (F29).

Subcommands
-----------
``provenance``  List every page's ``derived_from`` list with ``--orphan`` and ``--page`` filters.

The sub-app is registered onto the root ``app`` from
:mod:`lies.cli.__init__` (like the other ``*_app`` sub-apps). Heavy
imports (catalog, helper) stay inside the command body so ``import
lies.cli`` does not pull sqlite / orchestrator / pydantic_ai.

Note on structure: typer 0.27 flattens any ``Typer`` instance with
exactly one ``@command`` into a single command rather than a group.
The ``wiki_app`` registered onto the root ``app`` IS the
``provenance`` command's surface; ``CliRunner`` invokes it directly
without a ``provenance`` prefix. This will be revisited if a second
``wiki`` subcommand is added (multi-command group restores normal
dispatch).
"""

from __future__ import annotations

from typing import Annotated

import typer

__all__ = ("wiki_app",)


def _validate_page_slug(raw: str) -> str:
    """Trim + reject empty / whitespace-only; return the cleaned slug.

    Slashes ARE allowed because catalog slugs are path-shaped
    (``claude-code/concepts/hooks``). Whitespace inside a slug is not
    legal in the catalog, so any embedded whitespace is rejected.
    """
    cleaned = raw.strip()
    if not cleaned or any(c.isspace() for c in cleaned):
        typer.echo(f"error: invalid page slug: {raw!r}", err=True)
        raise typer.Exit(code=2)
    return cleaned


wiki_app = typer.Typer(
    name="wiki",
    help="Read views over the wiki.",
    rich_help_panel="Wiki management",
)


@wiki_app.command("provenance")
def provenance(
    page: Annotated[
        str | None,
        typer.Option(
            "--page",
            help="Restrict to one slug. Always emits JSON, even without --json.",
        ),
    ] = None,
    orphan: Annotated[
        bool,
        typer.Option(
            "--orphan",
            help="Show only pages with at least one dangling source slug.",
        ),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Emit JSON array instead of TSV.",
        ),
    ] = False,
    name: Annotated[
        str | None,
        typer.Option(
            "--name",
            envvar="LIES_WIKI_NAME",
            help="Wiki to inspect (default: $LIES_WIKI_NAME).",
        ),
    ] = None,
) -> None:
    """List every page's ``derived_from`` list (default: synthesised pages only)."""
    from lies.cli import resolve_wiki
    from lies.memory.catalog import open_catalog
    from lies.memory.provenance import (
        list_provenance_pages,
        render_provenance_json,
        render_provenance_tsv,
    )

    wiki = resolve_wiki(name)
    cleaned_page = _validate_page_slug(page) if page is not None else None

    conn = open_catalog(wiki)
    try:
        records = list_provenance_pages(conn, page=cleaned_page, orphan=orphan)
        if cleaned_page is not None and not records:
            # Distinguish "slug doesn't exist" from "slug exists but
            # orphan filter excluded it". Look up the slug directly.
            any_match = list_provenance_pages(conn, page=cleaned_page, orphan=False)
            if not any_match:
                typer.echo(f"error: page {cleaned_page} not found", err=True)
                raise typer.Exit(code=2)
            # Page exists but didn't match the orphan filter — return [].
    finally:
        conn.close()

    if cleaned_page is not None or json_output:
        typer.echo(render_provenance_json(records))
        return
    typer.echo(render_provenance_tsv(records))
