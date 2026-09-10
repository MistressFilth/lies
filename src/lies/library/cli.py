"""``lies ingest`` CLI: single command, --source / --batch modes.

This module is a thin Typer wrapper around the deterministic ingest
pipeline (:mod:`lies.library.ingest`). The command is registered
directly on the root ``lies`` app via :func:`register`, mirroring the
pattern used by ``sync`` and ``reindex`` in
:mod:`lies.cli.ingestion`. The canonical user-facing invocation is
``lies ingest --source <PATH>`` (or ``--batch <DIR>``); there is no
intermediate sub-app wrapper.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

__all__ = ("register",)


def _coerce_source(value: str | Path | None) -> Path | str | None:
    """Coerce a CLI string into Path (filesystem) or str (URL).

    ``Path("https://example.com/...")`` mangles the URL on POSIX
    (``PosixPath('https:/example.com/...')`` — single slash after the
    scheme), which the URL-prefix check in ``pick_scraper`` rejects.
    We accept ``str | Path`` on the wire and resolve to a ``Path``
    only when the value points at an existing filesystem entry.
    """
    if value is None:
        return None
    if isinstance(value, Path):
        return value
    try:
        p = Path(value)
    except (TypeError, ValueError):
        return value
    if p.exists():
        return p
    return value


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
            "Two modes supported: single source and batch. "
            "No LLM call on the ingest path."
        ),
        short_help="Ingest a single source (or many in batch mode) into the library.",
        rich_help_panel="Source ingestion",
    )
    def ingest(
        source: Annotated[
            str | None,
            typer.Option(
                "--source",
                help="Single source: file path or URL.",
            ),
        ] = None,
        batch: Annotated[
            str | None,
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
        coerced_source = _coerce_source(source)
        coerced_batch = _coerce_source(batch)
        # Minor 42: surface a clearer error when the operator passes
        # neither ``--collection`` nor ``--slug-prefix`` and there is
        # no batch parent dir to derive one from. Previously this
        # silently fell through to ``"default"`` — surprising for a
        # bare URL where the operator had no reason to expect a
        # collection called ``default``.
        coll_name = (
            collection
            or slug_prefix
            or (Path(coerced_batch).name if isinstance(coerced_batch, Path) else None)
        )
        if coll_name is None:
            typer.echo(
                "error: pass --collection <NAME> or --slug-prefix <NAME>; "
                "no collection name could be derived from the source.",
                err=True,
            )
            raise typer.Exit(code=2)
        # ``set`` is unordered; convert to a sorted ``list`` so the
        # Sequence-typed ``exclude_stems`` / ``exclude_dirs`` parameters
        # receive a deterministic iteration order.
        stems_list = list(exclude_stem)
        dirs_list = list(exclude_dir)

        if coerced_source is not None:
            result = run_source_ingest(
                lib,
                coll_name,
                coerced_source,
                fetcher=fetcher,
                slug=slug,
                title=title,
                exclude_stems=stems_list,
                exclude_dirs=dirs_list,
                force=force,
                dry_run=dry_run,
            )
        else:
            assert coerced_batch is not None
            result = run_batch_ingest(
                lib,
                coll_name,
                coerced_batch,
                fetcher=fetcher,
                exclude_stems=stems_list,
                exclude_dirs=dirs_list,
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
