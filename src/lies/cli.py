"""Typer CLI entrypoint."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Annotated, Any, cast

import typer
from rich.console import Console
from rich.markdown import Markdown

from lies import __version__
from lies.config import get_model, get_wiki_root
from lies.mcp import daemon
from lies.orchestrator import Orchestrator
from lies.qmd import daemon as qmd_daemon
from lies.qmd import qmd_status
from lies.schema.loader import load_default_schema
from lies.scrapers.base import pick_scraper
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


mcp_app = typer.Typer(
    name="mcp",
    help="Run the MCP server on stdio, or manage the http daemon.",
)
app.add_typer(mcp_app, name="mcp")


def _run_stdio() -> None:
    """Run the FastMCP server on stdio in the foreground."""
    configure_logging()
    from lies.mcp.server import mcp as _mcp_server

    _mcp_server.run(transport="stdio")


@mcp_app.callback(invoke_without_command=True)
def mcp_main(ctx: typer.Context) -> None:
    """Run the LIES MCP server on stdio.

    Spawn this process from any MCP-capable host (Claude Code, Cursor,
    etc.) to expose LIES tools and resources over the Model Context
    Protocol. See README "Using LIES from Claude Code" for registration
    commands.

    Bare ``lies mcp`` keeps running stdio for backward compatibility —
    every already-registered host invokes it that way. ``lies mcp start``
    is the explicit spelling of the same thing.
    """
    if ctx.invoked_subcommand is None:
        _run_stdio()


@mcp_app.command()
def start() -> None:
    """Run the MCP server on stdio in the foreground (same as bare `lies mcp`)."""
    _run_stdio()


@mcp_app.command(name="_serve", hidden=True)
def _serve(
    host: str = daemon.DEFAULT_HOST,
    port: int = daemon.DEFAULT_PORT,
) -> None:
    """Internal: run the MCP server on streamable-http in the foreground.

    Invoked only by ``lies mcp up`` through a re-exec. Not part of the
    supported surface; use ``up`` instead.
    """
    configure_logging()
    from lies.mcp.server import mcp as _mcp_server

    _mcp_server.run(transport="http", host=host, port=port)


@mcp_app.command()
def up(
    host: str = daemon.DEFAULT_HOST,
    port: int = daemon.DEFAULT_PORT,
    timeout: float = typer.Option(10.0, help="Seconds to wait for the port to accept."),
    no_qmd: bool = typer.Option(False, "--no-qmd", help="Skip ensuring qmd's daemon."),
    wiki_root: Path = typer.Option(None, "--wiki-root", "-w"),  # noqa: B008
) -> None:
    """Start a detached streamable-http MCP daemon for this wiki.

    Also ensures qmd's own daemon is running. qmd is a search backend,
    not a prerequisite: if it cannot be started the LIES daemon still
    comes up and a single warning goes to stderr.
    """
    configure_logging()
    root = _wiki_root_opt(wiki_root)
    try:
        rec = daemon.spawn_daemon(root, host=host, port=port, timeout=timeout)
    except daemon.DaemonAlreadyRunning as exc:
        typer.echo(str(exc))
        return
    except daemon.DaemonStartFailed as exc:
        typer.echo(f"error: {exc}", err=True)
        for line in daemon.tail_log(root, 20):
            typer.echo(line, err=True)
        raise typer.Exit(code=1) from exc
    except (daemon.PortUnavailable, daemon.DaemonBusy) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"lies mcp daemon running at {daemon.daemon_url(rec)} (pid {rec.pid})")

    if no_qmd:
        return
    qmd_state = qmd_daemon.ensure_qmd_daemon()
    if qmd_state.running:
        typer.echo(qmd_state.detail)
    else:
        typer.echo(f"warning: {qmd_state.detail}", err=True)


@mcp_app.command()
def down(
    grace: float = typer.Option(10.0, help="Seconds to wait after SIGTERM before SIGKILL."),
    wiki_root: Path = typer.Option(None, "--wiki-root", "-w"),  # noqa: B008
) -> None:
    """Stop the MCP daemon tracked for this wiki.

    Only pidfile-tracked daemons are stopped. Stdio servers spawned by an
    MCP host are never touched, and qmd's daemon is never touched at all:
    it is machine-global and shared with other wikis and tools.
    """
    configure_logging()
    root = _wiki_root_opt(wiki_root)
    try:
        result = daemon.stop_daemon(root, grace=grace)
    except (daemon.DaemonBusy, daemon.DaemonStopFailed) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    if result.action == "none":
        typer.echo("no daemon running")
    elif result.action == "cleared_stale":
        typer.echo(f"cleared stale pidfile for pid {result.pid}")
    else:
        typer.echo(f"stopped daemon pid {result.pid} ({result.signal})")


@mcp_app.command(name="status")
def _mcp_status(
    wiki_root: Path = typer.Option(None, "--wiki-root", "-w"),  # noqa: B008
) -> None:
    """Report whether an MCP daemon is running for this wiki.

    Exit code follows the ``systemctl is-active`` convention: 0 when
    running, 1 when stopped or stale, so shell callers can branch on it.
    """
    configure_logging()
    root = _wiki_root_opt(wiki_root)
    result = daemon.daemon_status(root)
    qmd_state = qmd_daemon.qmd_daemon_state()
    if result.running and result.record is not None:
        uptime = int(result.uptime_s or 0)
        typer.echo("status:  running")
        typer.echo(f"pid:     {result.record.pid}")
        typer.echo(f"url:     {result.url}")
        typer.echo(f"uptime:  {uptime}s")
        typer.echo(f"log:     {result.log}")
        typer.echo(f"qmd:     {qmd_state.detail}")
        return
    if result.stale and result.record is not None:
        typer.echo(f"status:  stopped (stale pidfile for pid {result.record.pid})")
        typer.echo(f"log:     {result.log}")
        typer.echo(f"qmd:     {qmd_state.detail}")
        raise typer.Exit(code=1)
    typer.echo("status:  stopped")
    typer.echo(f"log:     {result.log}")
    typer.echo(f"qmd:     {qmd_state.detail}")
    raise typer.Exit(code=1)


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
    source: str | None = None,
    prompt: str | None = None,
    apply: bool = False,
) -> None:
    """Inspect, modify, and author collection configurations."""
    import json
    import os
    from datetime import datetime, timezone

    import yaml  # type: ignore[import-untyped]
    from rich.prompt import Prompt

    from lies.collections.record import (
        Collection,
        load_collection,
        save_collection,
    )

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
    elif action == "new" and name:
        # Import the agent inside the branch so that tests can mock
        # ``lies.agents.collection_author.collection_author_agent`` at
        # the source module. Module-level imports would freeze the
        # reference before the mock applies.
        from lies.agents.collection_author import (
            AuthorProposal as _AuthorProposal,
        )
        from lies.agents.collection_author import (
            AuthorQuestion as _AuthorQuestion,
        )
        from lies.agents.collection_author import (
            CollectionAuthorDeps as _AuthorDeps,
        )
        from lies.agents.collection_author import (
            collection_author_agent as _factory,
        )

        if not source or not prompt:
            raise typer.BadParameter("collections new requires --source and --prompt")
        # Manifest-only fetch (no body). The scraper's emit_manifest
        # expects a list of ParsedDoc; an empty list produces an empty
        # manifest, which is fine — the agent uses it to ask format
        # questions and the user supplies the rest.
        scraper = pick_scraper(source)
        scratch_dir = wiki_root / ".lies" / "scratch"
        manifest_path = scraper.emit_manifest([], scratch_dir)
        manifest: list[dict[str, object]] = []
        if manifest_path and manifest_path.exists():
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest = list(data.get("files", []))
        agent = _factory()
        history: list[object] = []
        deps = _AuthorDeps(manifest=manifest)
        while True:
            # ``message_history`` expects a typed Sequence of model
            # messages; we accept arbitrary user-prompt injections from
            # the rich-prompt loop, so cast to Any at the boundary.
            result = agent.run_sync(
                prompt,
                deps=deps,
                message_history=cast(Any, history),
            )
            history.append(result.new_messages())
            out = result.output
            if isinstance(out, _AuthorQuestion):
                if out.options:
                    answer = Prompt.ask(
                        out.prompt,
                        choices=out.options,
                        default=out.default or out.options[0],
                    )
                elif out.default is not None:
                    answer = Prompt.ask(out.prompt, default=out.default)
                else:
                    answer = Prompt.ask(out.prompt)
                history.append({"role": "user", "content": f"{out.id}: {answer}"})
                continue
            if isinstance(out, _AuthorProposal):
                typer.echo(yaml.safe_dump(out.collection, sort_keys=True))
                if apply:
                    now = datetime.now(tz=timezone.utc)
                    payload = dict(out.collection)
                    payload.setdefault("name", name)
                    payload.setdefault("path", str(wiki_root / "raw" / name))
                    payload.setdefault("created_at", now)
                    payload.setdefault("updated_at", now)
                    # The agent may emit ISO strings; coerce to datetime
                    # so Collection's typed fields and save_collection's
                    # .isoformat() call work either way.
                    created = payload.get("created_at")
                    updated = payload.get("updated_at")
                    if isinstance(created, str):
                        payload["created_at"] = datetime.fromisoformat(
                            created.replace("Z", "+00:00")
                        )
                    if isinstance(updated, str):
                        payload["updated_at"] = datetime.fromisoformat(
                            updated.replace("Z", "+00:00")
                        )
                    payload["path"] = Path(payload["path"])
                    doc_path = payload.get("doc_path")
                    if doc_path is not None:
                        payload["doc_path"] = Path(doc_path)
                    collection = Collection(**payload)
                    save_collection(wiki_root, collection)
                    typer.echo(f"wrote {cfg_dir / (name + '.yaml')}")
                return
            raise typer.BadParameter("agent returned unexpected output")
    else:
        raise typer.BadParameter(f"unknown action: {action}")


if __name__ == "__main__":
    app()
