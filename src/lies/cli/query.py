"""Querying and maintenance panel: query, lint, status.

Orchestrator + qmd + rich imports stay inside command bodies so
``import lies.cli`` doesn't pay for the model stack or the rich
markdown renderer (markdown-it is ~30ms of cold-start).
"""

from __future__ import annotations

from typing import Annotated

import typer

from lies.cli import app
from lies.cli._helpers import (
    WikiFlockUnrepairable,
    WikiLockBusy,
    configure_logging,
)

__all__ = (
    "lint",
    "query",
    "status",
)


@app.command(
    short_help="Query the wiki with LLM synthesis over qmd hits (extractive fallback).",
    rich_help_panel="Querying and maintenance",
)
def query(
    question: str = typer.Argument(..., help="The question to ask the wiki."),
    collection: str | None = typer.Option(
        None,
        "--collection",
        help=(
            "Collection the synthesized page is filed under "
            "(wiki/<collection>/synthesis/<file>). Required for the file-back "
            "loop to actually write; without it, the agent's should_file "
            "verdict is recorded as a synthesis_reason note."
        ),
    ),
    no_file: bool = typer.Option(
        False,
        "--no-file",
        help="Skip the file-back loop even if the agent marks the answer should_file.",
    ),
    force_file: bool = typer.Option(
        False,
        "--force-file",
        help="Force the file-back loop even if the agent did not mark should_file.",
    ),
    name: str | None = typer.Option(
        None, "--name", envvar="LIES_WIKI_NAME", help="Wiki to query (default: $LIES_WIKI_NAME)."
    ),
) -> None:
    """Query the wiki with LLM synthesis over qmd hits, with an extractive fallback."""
    from rich.console import Console
    from rich.markdown import Markdown

    from lies.cli import Orchestrator, resolve_wiki
    from lies.memory.models import WikiPlanInvalid

    configure_logging()
    wiki = resolve_wiki(name)
    orch = Orchestrator(wiki)
    # Use the host-side ``run_query`` entry point so LLM synthesis runs
    # with the qmd->index retrieval and the extractive fallback intact.
    # ``--no-file`` maps to ``file=False``; ``--force-file`` to
    # ``force_file=True``; ``--collection`` flows straight through so the
    # orchestrator can route the new page under the right wiki subdir.
    try:
        answer = orch.run_query(
            question,
            collection=collection,
            file=not no_file,
            force_file=force_file,
        )
    except WikiPlanInvalid as exc:
        # ``run_query`` raises when the agent/force file marked the answer
        # for filing but the caller did not supply ``--collection``.
        # Without the typed-error envelope a missing collection would
        # silently drop the filing intent; the spec mandates a clean
        # exit-2 + operator-actionable message instead.
        typer.echo(
            "error: --collection NAME required to file synthesis (or pass --no-file to skip)",
            err=True,
        )
        raise typer.Exit(code=2) from exc
    console = Console()
    console.print(Markdown(answer.answer))
    if answer.synthesis_reason:
        if answer.synthesis_used:
            console.print(Markdown(f"_Note: {answer.synthesis_reason}._"))
        else:
            console.print(
                Markdown(
                    f"_Note: LLM synthesis unavailable ({answer.synthesis_reason}); "
                    f"answered extractively._"
                )
            )
    # F3 file-back receipt. Printed only when there is something to say
    # (durable change or error); an empty receipt is silent so the no-op
    # case stays clean.
    if answer.file_receipt:
        if answer.file_receipt.changed_pages:
            lines = ["(synthesis: durably filed"]
            for ref in answer.file_receipt.changed_pages:
                lines.append(f"  - {ref.op.value}: {ref.path}")
            lines.append(")")
            typer.echo("\n".join(lines))
        elif answer.file_receipt.errors:
            typer.echo(f"(synthesis: error — {answer.file_receipt.errors[0]})")


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
    from rich.console import Console
    from rich.markdown import Markdown

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
    Console().print(Markdown(output))


@app.command(
    short_help="Show qmd status, recent invisible writes, and the last few log entries.",
    rich_help_panel="Querying and maintenance",
)
def status(
    name: str | None = typer.Option(
        None,
        "--name",
        envvar="LIES_WIKI_NAME",
        help="Wiki to report status for (default: $LIES_WIKI_NAME).",
    ),
    memory_limit: int = typer.Option(
        10,
        "--memory-limit",
        help="Number of recent MemoryPlan applications to show. Pass 0 to skip.",
    ),
) -> None:
    """Show qmd status, recent invisible writes, and the last few log entries."""
    from lies.cli import resolve_wiki
    from lies.memory import sidecar
    from lies.qmd import qmd_status
    from lies.wiki.layout import WikiLayout

    if memory_limit < 0:
        raise typer.BadParameter("--memory-limit must be >= 0", param_hint="--memory-limit")
    configure_logging()
    wiki = resolve_wiki(name)
    root = wiki.data_root
    layout = WikiLayout(root)
    try:
        from lies.memory.catalog import count_pages, open_catalog

        conn = open_catalog(wiki)
        try:
            n_pages = count_pages(conn)
            ver_row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
            schema_ver = ver_row[0] if ver_row else "unknown"
        finally:
            conn.close()
        typer.echo(f"catalog: {n_pages} pages, schema v{schema_ver}")
    except Exception:  # noqa: BLE001 - status is observability; never fail the command
        typer.echo("catalog: unavailable")
    typer.echo("=== qmd status ===")
    try:
        typer.echo(qmd_status(root))
    except Exception as exc:  # noqa: BLE001 - qmd failures must not crash the CLI
        typer.echo(f"qmd unavailable: {exc}")
    if memory_limit > 0:
        try:
            rows = sidecar.read_recent(wiki, limit=memory_limit)
        except OSError as exc:
            typer.echo("\n=== recent invisible writes ===")
            typer.echo(f"sidecar unavailable: {exc}")
        else:
            if rows:
                typer.echo("\n=== recent invisible writes ===")
                for rec in rows:
                    typer.echo(sidecar.format_record_block(rec))
    typer.echo("\n=== last 10 log entries ===")
    log_path = layout.wiki_dir / "log.md"
    if log_path.exists():
        lines = log_path.read_text().splitlines()
        for line in lines[-10:]:
            typer.echo(line)
    else:
        typer.echo("(no log yet)")
