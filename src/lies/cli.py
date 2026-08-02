"""Typer CLI entrypoint."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markdown import Markdown

from lies import __version__
from lies.config import get_model, get_wiki_root
from lies.orchestrator import Orchestrator
from lies.qmd import qmd_status
from lies.schema.loader import load_default_schema
from lies.utils.logging import configure_logging
from lies.wiki.git import atomic_commit
from lies.wiki.layout import WikiLayout

app = typer.Typer(
    name="lies",
    help="Library of Inconsistent Explanations & Sources — a Karpathy-pattern LLM wiki.",
    # No `no_args_is_help=True`: with no subcommand, the callback's REPL runs.
    # Setting both would suppress the REPL and dump help on bare `lies`.
)
console = Console()


def _wiki_root_opt(wiki_root: Path | None) -> Path:
    """Resolve the --wiki-root option, defaulting to env or cwd."""
    if wiki_root is not None:
        return wiki_root.resolve()
    return get_wiki_root()


@app.command()
def version() -> None:
    """Print the LIES version and exit."""
    typer.echo(f"lies {__version__}")


@app.command()
def mcp() -> None:
    """Run the LIES MCP server on stdio.

    Spawn this process from any MCP-capable host (Claude Code,
    Cursor, etc.) to expose LIES tools and resources over the Model
    Context Protocol. See README "Using LIES from Claude Code" for
    registration commands.
    """
    configure_logging()
    from lies.mcp.server import mcp as _mcp_server

    _mcp_server.run(transport="stdio")


@app.command()
def config() -> None:
    """Print the current LIES configuration."""
    typer.echo(f"model: {get_model()}")
    typer.echo(f"wiki_root: {get_wiki_root()}")


@app.command()
def init(
    path: Path = typer.Argument(..., help="Where to create the new wiki."),  # noqa: B008
) -> None:
    """Initialize a new LIES wiki at <path>."""
    configure_logging()
    target = path.resolve()
    if target.exists() and any(target.iterdir()):
        raise typer.BadParameter(f"{target} is not empty")
    target.mkdir(parents=True, exist_ok=True)
    layout = WikiLayout(target)
    layout.init()
    # Copy default schema to .lies/schema.md so the user can edit
    layout.schema_path.write_text(load_default_schema(), encoding="utf-8")
    # Initialize git
    subprocess.run(["git", "init", "--initial-branch=main", str(target)], check=True)
    subprocess.run(["git", "config", "user.email", "lies@local"], cwd=target, check=True)
    subprocess.run(["git", "config", "user.name", "LIES"], cwd=target, check=True)
    # Initial commit
    subprocess.run(["git", "add", "."], cwd=target, check=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit: empty LIES wiki"], cwd=target, check=True
    )
    typer.echo(f"Initialized wiki at {target}")


@app.command()
def ingest(
    collection: str,
    *,
    source: str | None = None,
    model: str | None = None,
) -> None:
    """Ingest a source into a collection (creates collection if missing).

    First-time flow (LLM scraper generation) deferred to follow-up.
    Currently behaves like sync for existing collections.

    Errors if the collection YAML is missing; ``sync_helper.sync_collection``
    does not auto-scaffold a collection from a source path. Use
    ``ingest_source`` for the legacy single-source ingestion path.
    """
    import os

    from lies.etl.sync_helper import (
        acquire_heartbeat,
        release_heartbeat,
        sync_collection,
    )

    wiki_root = Path(os.environ.get("LIES_WIKI_ROOT", ".")).resolve()
    if acquire_heartbeat(wiki_root, wait=False, fail_busy=True) is None:
        raise typer.Exit(code=2)
    try:
        sync_collection(wiki_root, collection, force=False)
    finally:
        release_heartbeat(wiki_root)


@app.command()
def ingest_source(
    source: str = typer.Argument(..., help="Path, URL, or '-' for stdin."),
    wiki_root: Path = typer.Option(None, "--wiki-root", "-w"),  # noqa: B008
) -> None:
    """Atomic ingest of a single source into the wiki at ``wiki_root``.

    Kept for backward compatibility with the original source-path CLI
    surface (``lies ingest <source>``). Delegates to
    :meth:`Orchestrator.run_ingest`, which snapshots the working
    tree, runs the agent, and commits atomically. On any failure the
    working tree is restored and the exception is re-raised.
    """
    configure_logging()
    root = _wiki_root_opt(wiki_root)
    orch = Orchestrator(wiki_root=root)
    output = orch.run_ingest(source)
    typer.echo(output)


@app.command()
def query(
    question: str = typer.Argument(..., help="The question to ask the wiki."),
    wiki_root: Path = typer.Option(None, "--wiki-root", "-w"),  # noqa: B008
) -> None:
    """Query the wiki with qmd → index.md fallback."""
    configure_logging()
    root = _wiki_root_opt(wiki_root)
    orch = Orchestrator(wiki_root=root)
    # Use the host-side ``run_query`` entry point so the synthesizer with
    # qmd→index fallback runs without an LLM round-trip.
    answer = orch.run_query(question)
    console.print(Markdown(answer.answer))


@app.command()
def lint(
    wiki_root: Path = typer.Option(None, "--wiki-root", "-w"),  # noqa: B008
    fix: bool = typer.Option(False, "--fix", help="Apply repair plan for safe_to_fix findings."),
) -> None:
    """Run lint; with --fix also apply the repair plan."""
    configure_logging()
    root = _wiki_root_opt(wiki_root)
    orch = Orchestrator(wiki_root=root)
    # Use the host-side ``run_lint`` entry point so the lint pass writes
    # a deterministic ``wiki/lint-report.md`` and appends to ``wiki/log.md``.
    output = orch.run_lint(apply=fix)
    console.print(Markdown(output))


@app.command()
def status(
    wiki_root: Path = typer.Option(None, "--wiki-root", "-w"),  # noqa: B008
) -> None:
    """Show qmd status and the last few log entries."""
    configure_logging()
    root = _wiki_root_opt(wiki_root)
    layout = WikiLayout(root)
    typer.echo("=== qmd status ===")
    try:
        typer.echo(qmd_status(root))
    except Exception as exc:  # noqa: BLE001 - qmd failures must not crash the CLI
        typer.echo(f"qmd unavailable: {exc}")
    typer.echo("\n=== last 10 log entries ===")
    if layout.log_path.exists():
        lines = layout.log_path.read_text().splitlines()
        for line in lines[-10:]:
            typer.echo(line)
    else:
        typer.echo("(no log yet)")


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    wiki_root: Path = typer.Option(None, "--wiki-root", "-w", envvar="LIES_WIKI_ROOT"),  # noqa: B008
    no_memory: bool = typer.Option(
        False,
        "--no-memory",
        help="Disable invisible wiki memory for free-form REPL commands.",
    ),
) -> None:
    """REPL mode when no subcommand is given."""
    if ctx.invoked_subcommand is not None:
        return
    configure_logging()
    root = _wiki_root_opt(wiki_root)
    orch = Orchestrator(wiki_root=root)
    console.print("[bold]LIES REPL[/bold] — type /help for commands, /exit to leave.")
    while True:
        try:
            line = console.input("lies> ")
        except (EOFError, KeyboardInterrupt):
            break
        line = line.strip()
        if not line:
            continue
        if line in ("/exit", "/quit"):
            break
        if line == "/help":
            console.print(
                "Commands:\n"
                "  /ingest <source>   Add a source to the wiki\n"
                "  /query <question>  Ask a question\n"
                "  /lint              Health-check the wiki\n"
                "  /status            qmd status + last 10 log entries\n"
                "  /commit            Force a git commit\n"
                "  /exit              Leave the REPL"
            )
            continue
        if line == "/commit":
            try:
                sha = atomic_commit(root, "manual commit")
                typer.echo(f"committed {sha[:8]}")
            except Exception as exc:  # noqa: BLE001 - commit failures must not crash the REPL
                typer.echo(f"commit failed: {exc}")
            continue
        # Otherwise, dispatch as a free-form command
        output = orch.run(line) if no_memory else orch.run_with_memory(line)
        console.print(Markdown(output))
    console.print("\nbye.")


@app.command()
def sync(
    collection: Annotated[str | None, typer.Argument()] = None,
    *,
    force: bool = False,
    wait: bool = False,
    fail_busy: bool = False,
) -> None:
    """Sync one or all collections."""
    import os

    from lies.etl.sync_helper import (
        acquire_heartbeat,
        collection_names,
        release_heartbeat,
        sync_collection,
    )

    wiki_root = Path(os.environ.get("LIES_WIKI_ROOT", ".")).resolve()
    if acquire_heartbeat(wiki_root, wait=wait, fail_busy=fail_busy) is None:
        raise typer.Exit(code=2)
    try:
        for name in collection_names(wiki_root, collection):
            sync_collection(wiki_root, name, force=force)
    finally:
        release_heartbeat(wiki_root)


@app.command()
def reindex(
    *,
    reconcile: bool = False,
    embed: bool = False,
    force: bool = False,
    cleanup: bool = False,
    all: bool = False,
) -> None:
    """Reindex QMD collections.

    ``--reconcile`` syncs each collection (running the full pipeline).
    ``--embed`` and ``--cleanup`` are currently no-op placeholders
    pending upstream qmd support; passing them prints a stderr warning
    but does not fail.
    """
    import os

    from lies.etl.sync_helper import collection_names, sync_collection

    if all:
        reconcile, embed, cleanup = True, True, True
    if force and not embed:
        raise typer.BadParameter("--force requires --embed")

    wiki_root = Path(os.environ.get("LIES_WIKI_ROOT", ".")).resolve()
    if reconcile:
        for name in collection_names(wiki_root, None):
            sync_collection(wiki_root, name, force=False)
    if embed:
        from lies.qmd.cli import qmd_embed

        qmd_embed(wiki_root, force=force)
        typer.echo(
            "warning: --embed is a no-op; upstream qmd has no embed subcommand yet.",
            err=True,
        )
    if cleanup:
        from lies.qmd.cli import qmd_cleanup

        qmd_cleanup(wiki_root)
        typer.echo(
            "warning: --cleanup is a no-op; upstream qmd has no cleanup subcommand yet.",
            err=True,
        )


@app.command()
def collections(
    action: Annotated[str, typer.Argument()],
    name: Annotated[str | None, typer.Argument()] = None,
    *,
    tag: str | None = None,
) -> None:
    """Inspect and modify collection configurations."""
    import os

    from lies.collections.record import load_collection

    wiki_root = Path(os.environ.get("LIES_WIKI_ROOT", ".")).resolve()
    cfg_dir = wiki_root / ".lies" / "collections"
    if action == "list":
        for p in sorted(cfg_dir.glob("*.yaml")):
            print(p.stem)
    elif action == "show" and name:
        from lies.memory.service import WikiMemoryService
        from lies.wiki.layout import WikiLayout

        c = load_collection(wiki_root, name)
        print(f"name={c.name} source={c.source} tags={c.tags}")
        # The CLI doesn't know whether sync has run in this process;
        # an empty registry means the in-process WikiMemoryService for
        # this wiki root has not registered any collection yet.
        layout = WikiLayout(wiki_root)
        svc = WikiMemoryService(layout)
        registered = svc.registered_collections()
        ref = next(
            (r for r in registered if r.collection_id == name),
            None,
        )
        print(f"status: {'registered' if ref else 'pending'}")
    elif action == "modify" and name:
        raise typer.BadParameter(
            f"`lies collections modify {name}` is not implemented yet; "
            f"edit `<wiki>/.lies/collections/{name}.yaml` by hand for now."
        )
    else:
        raise typer.BadParameter(f"unknown action: {action}")


if __name__ == "__main__":
    app()
