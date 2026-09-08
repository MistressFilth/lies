"""``lies ingest`` CLI: single command, --source / --batch modes.

This module is a thin Typer wrapper around the deterministic ingest
pipeline (:mod:`lies.library.ingest`). The command is registered
directly on the root ``lies`` app via :func:`register`, mirroring the
pattern used by ``sync`` and ``reindex`` in
:mod:`lies.cli.ingestion`. The canonical user-facing invocation is
``lies ingest --source <PATH>`` (or ``--batch <DIR>``); there is no
intermediate sub-app wrapper.

The legacy ``lies ingest-source`` and the ``--no-llm`` opt-out are
deprecated (see :mod:`lies.cli.ingestion`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

__all__ = ("register",)


def register(app: typer.Typer) -> None:
    """Register the ``ingest`` command on ``app``.

    Mirrors the ``sync`` / ``reindex`` registration pattern in
    :mod:`lies.cli.ingestion`: a direct ``@app.command(...)`` decorator
    on the root Typer app, not a sub-app. This makes the canonical
    ``lies ingest --source <PATH>`` invocation reachable.
    """

    @app.command(
        name="ingest",
        help=(
            "Deterministic ingest into the library. "
            "Two modes: --source (single) and --batch (multi). "
            "No LLM call on the ingest path."
        ),
        short_help="Ingest one source (--source) or many (--batch) into the library.",
        rich_help_panel="Source ingestion",
    )
    def ingest(
        source: Annotated[
            Path | None,
            typer.Option(
                "--source",
                help="Single source: file path or URL.",
            ),
        ] = None,
        batch: Annotated[
            Path | None,
            typer.Option(
                "--batch",
                help="Directory to walk for batch ingest.",
            ),
        ] = None,
        slug_prefix: Annotated[
            str | None,
            typer.Option(
                "--slug-prefix",
                help="Collection name (batch mode; defaults to source parent dir).",
            ),
        ] = None,
        collection: Annotated[
            str | None,
            typer.Option(
                "--collection",
                help="Target collection name. Alias for --slug-prefix (single mode).",
            ),
        ] = None,
        slug: Annotated[
            str | None,
            typer.Option(
                "--slug",
                help="Override slug (single-source mode).",
            ),
        ] = None,
        title: Annotated[
            str | None,
            typer.Option(
                "--title",
                help="Override title (single-source mode).",
            ),
        ] = None,
        exclude_stem: Annotated[
            list[str],
            typer.Option(
                "--exclude-stem",
                help="Additional filename stems to skip. Repeatable.",
            ),
        ] = [],
        exclude_dir: Annotated[
            list[str],
            typer.Option(
                "--exclude-dir",
                help="Additional filename prefixes to skip. Repeatable.",
            ),
        ] = [],
        force: Annotated[
            bool,
            typer.Option(
                "--force/--no-force",
                help="Overwrite existing mirrors (default: fail on collision).",
            ),
        ] = False,
        dry_run: Annotated[
            bool,
            typer.Option(
                "--dry-run/--no-dry-run",
                help="Print plan, write nothing.",
            ),
        ] = False,
    ) -> None:
        """Ingest a single source or a directory of sources into the library."""
        from lies.library.fetcher import ScraperFetcher
        from lies.library.ingest import run_batch_ingest, run_source_ingest
        from lies.library.paths import Library

        if source is None and batch is None:
            typer.echo("error: pass --source or --batch", err=True)
            raise typer.Exit(code=2)
        if source is not None and batch is not None:
            typer.echo("error: pass --source or --batch, not both", err=True)
            raise typer.Exit(code=2)

        lib = Library.open()
        fetcher = ScraperFetcher(library=lib)
        coll_name = collection or slug_prefix or (batch.name if batch else None) or "default"

        if source is not None:
            result = run_source_ingest(
                lib,
                coll_name,
                source,
                fetcher=fetcher,
                slug=slug,
                title=title,
                exclude_stems=set(exclude_stem),
                exclude_dirs=set(exclude_dir),
                force=force,
                dry_run=dry_run,
            )
        else:
            assert batch is not None
            result = run_batch_ingest(
                lib,
                coll_name,
                batch,
                fetcher=fetcher,
                exclude_stems=set(exclude_stem),
                exclude_dirs=set(exclude_dir),
                force=force,
                dry_run=dry_run,
            )

        summary = (
            f"created={result.created} updated={result.updated} "
            f"skipped={result.skipped} errors={result.errors}"
        )
        typer.echo(summary)
        if result.errors:
            raise typer.Exit(code=1)
