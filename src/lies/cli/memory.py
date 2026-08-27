"""`lies memory` — recent MemoryPlan applications from the sidecar.

Three subcommands:

- `lies memory` (default) — show last 10 applied plans.
- `lies memory reconcile` — rebuild the sidecar from `git log --grep='^memory:'`.
- `lies memory truncate --keep N [--force]` — cap the sidecar to N rows.

Filters `--limit`, `--pages`, `--ops`, `--since`, `--json` apply to the
default subcommand only.

Heavy ``lies.memory`` imports stay deferred. ``sidecar`` is exposed via
a module-level ``__getattr__`` so tests that patch
``lies.cli.memory.sidecar.<fn>`` keep working, but ``import lies.cli``
does not pay for the orchestrator / pydantic_ai / fastmcp stack — the
same contract that ``tests/unit/cli/test_cli_lazy_imports.py`` pins.
"""

from __future__ import annotations

import typer

from lies.wiki.wiki import Wiki

memory_app = typer.Typer(
    name="memory",
    help="Inspect recent MemoryPlan applications.",
    rich_help_panel="Querying and maintenance",
    no_args_is_help=False,
)


def __getattr__(name: str):
    """Lazy re-export of ``sidecar``.

    The brief's reconcile/truncate tests reach
    ``lies.cli.memory.sidecar.<fn>`` via ``mock.patch``. Loading
    ``sidecar`` here instead of at module top preserves the lazy-imports
    contract — ``import lies.cli`` does not pull pydantic_ai / fastmcp
    into ``sys.modules``. First access triggers the import (and caches
    the module on ``globals()`` so subsequent reads are a normal
    attribute access).
    """
    if name == "sidecar":
        import lies.memory.sidecar as _sidecar

        globals()[name] = _sidecar
        return _sidecar
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _resolve_wiki(name: str | None = None) -> Wiki:
    from lies.cli import resolve_wiki

    return resolve_wiki(name)


def _format_record(rec) -> str:
    """Render one MemoryPlanRecord as a 4-line block.

    Thin wrapper around :func:`lies.memory.sidecar.format_record_block`,
    the single source of truth for ``MemoryPlanRecord`` formatting. Kept
    here as a module-private shim so existing callers/tests don't need to
    change import paths.
    """
    from lies.memory.sidecar import format_record_block

    return format_record_block(rec)


@memory_app.callback(invoke_without_command=True)
def memory(
    ctx: typer.Context,
    name: str | None = typer.Option(
        None,
        "--name",
        envvar="LIES_WIKI_NAME",
        help="Wiki to inspect (default: $LIES_WIKI_NAME).",
    ),
    limit: int = typer.Option(10, "--limit", help="Last N records."),
    pages: str | None = typer.Option(None, "--pages", help="Filter by page substring."),
    ops: str | None = typer.Option(
        None, "--ops", help="Filter by op kind (comma-separated, any-of)."
    ),
    since: str | None = typer.Option(None, "--since", help="Filter by ts >= ISO timestamp."),
    json_output: bool = typer.Option(False, "--json", help="Emit raw JSONL, one line per record."),
) -> None:
    """Show recent MemoryPlan applications from the JSONL sidecar."""
    if ctx.invoked_subcommand is not None:
        return
    from lies.memory import sidecar

    wiki = _resolve_wiki(name)
    op_filters = [o.strip() for o in ops.split(",") if o.strip()] if ops else None
    try:
        rows = sidecar.read_recent(
            wiki,
            limit=limit,
            page=pages,
            op=op_filters[0] if op_filters else None,
            since=since,
        )
    except OSError as exc:
        typer.echo(f"sidecar unavailable: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    if json_output:
        for rec in rows:
            typer.echo(rec.model_dump_json())
        return
    if not rows:
        typer.echo("(no plans recorded yet)")
        return
    typer.echo("Last applied MemoryPlans:")
    for rec in rows:
        typer.echo(_format_record(rec))


@memory_app.command("reconcile")
def reconcile(
    name: str | None = typer.Option(
        None,
        "--name",
        envvar="LIES_WIKI_NAME",
        help="Wiki to inspect (default: $LIES_WIKI_NAME).",
    ),
) -> None:
    """Rebuild the sidecar from `git log --grep='^memory:'`."""
    from lies.memory import sidecar

    wiki = _resolve_wiki(name)
    n = sidecar.reconcile_from_git_log(wiki)
    typer.echo(f"reconciled: {n} row(s) written")


@memory_app.command("truncate")
def truncate(
    name: str | None = typer.Option(
        None,
        "--name",
        envvar="LIES_WIKI_NAME",
        help="Wiki to inspect (default: $LIES_WIKI_NAME).",
    ),
    keep: int = typer.Option(..., "--keep", help="Number of rows to keep."),
    force: bool = typer.Option(False, "--force", help="Allow --keep > current count."),
) -> None:
    """Cap the sidecar to its last N rows."""
    from lies.memory import sidecar

    wiki = _resolve_wiki(name)
    try:
        kept = sidecar.truncate(wiki, keep=keep, force=force)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"truncated: {kept} row(s) kept")
