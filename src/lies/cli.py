"""Typer CLI entrypoint."""
from __future__ import annotations

import subprocess
from pathlib import Path

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
    subprocess.run(["git", "commit", "-m", "Initial commit: empty LIES wiki"], cwd=target, check=True)
    typer.echo(f"Initialized wiki at {target}")


@app.command()
def ingest(
    source: str = typer.Argument(..., help="Path under the wiki raw/ directory."),
    wiki_root: Path = typer.Option(None, "--wiki-root", "-w"),  # noqa: B008
) -> None:
    """Ingest a source into the wiki."""
    configure_logging()
    root = _wiki_root_opt(wiki_root)
    orch = Orchestrator(wiki_root=root)
    # Use the host-side ``run_ingest`` entry point so the working tree is
    # snapshotted and rolled back if anything fails mid-ingest. The plain
    # ``orch.run`` is reserved for read-only operations.
    output = orch.run_ingest(source)
    console.print(Markdown(output))


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


if __name__ == "__main__":
    app()
