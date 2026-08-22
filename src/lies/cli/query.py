"""Querying and maintenance panel: query, lint, status.

Orchestrator + qmd imports stay inside command bodies so
``import lies.cli`` doesn't pay for the model stack.
"""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console
from rich.markdown import Markdown

from lies.cli import app
from lies.cli._helpers import (
    WikiFlockUnrepairable,
    WikiLockBusy,
    configure_logging,
)
from lies.wiki.layout import WikiLayout

__all__ = (
    "lint",
    "query",
    "status",
)

console = Console()


@app.command(
    short_help="Query the wiki with qmd -> index.md fallback.",
    rich_help_panel="Querying and maintenance",
)
def query(
    question: str = typer.Argument(..., help="The question to ask the wiki."),
    name: str | None = typer.Option(
        None, "--name", envvar="LIES_WIKI_NAME", help="Wiki to query (default: $LIES_WIKI_NAME)."
    ),
) -> None:
    """Query the wiki with qmd -> index.md fallback."""
    from lies.cli import Orchestrator, resolve_wiki

    configure_logging()
    wiki = resolve_wiki(name)
    orch = Orchestrator(wiki)
    # Use the host-side ``run_query`` entry point so the synthesizer with
    # qmd->index fallback runs without an LLM round-trip.
    answer = orch.run_query(question)
    console.print(Markdown(answer.answer))


@app.command(
    short_help="Run lint; with --fix also apply the repair plan.",
    rich_help_panel="Querying and maintenance",
)
def lint(
    name: Annotated[
        str | None,
        typer.Option(
            "--name",
            envvar="LIES_WIKI_NAME",
            help="Wiki to lint (default: $LIES_WIKI_NAME).",
        ),
    ] = None,
    fix: Annotated[
        bool, typer.Option("--fix", help="Apply repair plan for safe_to_fix findings.")
    ] = False,
    force_repair: Annotated[
        bool,
        typer.Option(
            "--force-repair",
            help=(
                "Reap a stale memory flock and retry once before applying the "
                "repair plan. Only meaningful with --fix; surfaces "
                "WikiFlockUnrepairable (exit 1) if the retry still loses."
            ),
        ),
    ] = False,
) -> None:
    """Run lint; with --fix also apply the repair plan.

    ``--force-repair`` (with ``--fix``) escalates wiki-memory
    contention: the cross-process flock is unconditionally reaped +
    retried once before applying the repair plan. Without the flag, a
    live contender surfaces as ``WikiLockBusy`` (exit 1). If the
    force-repair retry still loses, ``WikiFlockUnrepairable`` is
    surfaced (also exit 1) with an operator-actionable pointer to
    ``lies flock <name> force-repair``.
    """
    from lies.cli import Orchestrator, WikiLinkCorpusMissing, WikiLinkResolver, resolve_wiki

    configure_logging()
    wiki = resolve_wiki(name)
    try:
        resolver = WikiLinkResolver.build((wiki.wiki_dir, wiki.raw_dir))
    except WikiLinkCorpusMissing:
        typer.echo(f"error: no wiki/ or raw/ directory under {wiki.data_root}", err=True)
        raise typer.Exit(code=2) from None
    orch = Orchestrator(wiki)
    # Use the host-side ``run_lint`` entry point so the lint pass writes
    # a deterministic ``wiki/lint-report.md`` and appends to ``wiki/log.md``.
    try:
        output = orch.run_lint(apply=fix, resolver=resolver, force_repair=force_repair)
    except WikiFlockUnrepairable as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    except WikiLockBusy as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    console.print(Markdown(output))


@app.command(
    short_help="Show qmd status and the last few log entries.",
    rich_help_panel="Querying and maintenance",
)
def status(
    name: str | None = typer.Option(
        None,
        "--name",
        envvar="LIES_WIKI_NAME",
        help="Wiki to report status for (default: $LIES_WIKI_NAME).",
    ),
) -> None:
    """Show qmd status and the last few log entries."""
    from lies.cli import resolve_wiki
    from lies.qmd import qmd_status

    configure_logging()
    wiki = resolve_wiki(name)
    root = wiki.data_root
    layout = WikiLayout(root)
    typer.echo("=== qmd status ===")
    try:
        typer.echo(qmd_status(root))
    except Exception as exc:  # noqa: BLE001 - qmd failures must not crash the CLI
        typer.echo(f"qmd unavailable: {exc}")
    typer.echo("\n=== last 10 log entries ===")
    log_path = layout.wiki_dir / "log.md"
    if log_path.exists():
        lines = log_path.read_text().splitlines()
        for line in lines[-10:]:
            typer.echo(line)
    else:
        typer.echo("(no log yet)")
