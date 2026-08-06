"""Typer CLI entrypoint."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, cast

import typer
from rich.console import Console
from rich.markdown import Markdown

from lies import __version__, xdg
from lies.mcp import daemon
from lies.mcp.resolution import resolve_wiki
from lies.orchestrator import Orchestrator
from lies.providers import ProviderConfigError
from lies.qmd import daemon as qmd_daemon
from lies.qmd import qmd_status
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
    try:
        daemon.require_loopback_host(host)
    except daemon.NonLoopbackBind as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    configure_logging()
    from lies.mcp.server import mcp as _mcp_server

    _mcp_server.run(transport="http", host=host, port=port)


@mcp_app.command()
def up(
    host: str = daemon.DEFAULT_HOST,
    port: int = daemon.DEFAULT_PORT,
    timeout: float = typer.Option(10.0, help="Seconds to wait for the port to accept."),
    no_qmd: bool = typer.Option(False, "--no-qmd", help="Skip ensuring qmd's daemon."),
    name: str | None = typer.Option(None, "--name", envvar="LIES_WIKI_NAME"),
) -> None:
    """Start a detached streamable-http MCP daemon for this wiki.

    Also ensures qmd's own daemon is running. qmd is a search backend,
    not a prerequisite: if it cannot be started the LIES daemon still
    comes up and a single warning goes to stderr.
    """
    configure_logging()
    wiki = resolve_wiki(name)
    try:
        rec = daemon.spawn_daemon(wiki, host=host, port=port, timeout=timeout)
    except daemon.DaemonAlreadyRunning as exc:
        typer.echo(str(exc))
        return
    except daemon.DaemonStartFailed as exc:
        typer.echo(f"error: {exc}", err=True)
        for line in daemon.tail_log(wiki, 20):
            typer.echo(line, err=True)
        raise typer.Exit(code=1) from exc
    except (daemon.NonLoopbackBind, daemon.PortUnavailable, daemon.DaemonBusy) as exc:
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
    name: str | None = typer.Option(None, "--name", envvar="LIES_WIKI_NAME"),
) -> None:
    """Stop the MCP daemon tracked for this wiki.

    Only pidfile-tracked daemons are stopped. Stdio servers spawned by an
    MCP host are never touched, and qmd's daemon is never touched at all:
    it is machine-global and shared with other wikis and tools.
    """
    configure_logging()
    wiki = resolve_wiki(name)
    try:
        result = daemon.stop_daemon(wiki, grace=grace)
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
    name: str | None = typer.Option(None, "--name", envvar="LIES_WIKI_NAME"),
) -> None:
    """Report whether an MCP daemon is running for this wiki.

    Exit code follows the ``systemctl is-active`` convention: 0 when
    running, 1 when stopped or stale, so shell callers can branch on it.
    """
    configure_logging()
    wiki = resolve_wiki(name)
    result = daemon.daemon_status(wiki)
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
def migrate_xdg(legacy_path: Path, name: str, force: bool = False) -> None:
    """Migrate a legacy ``.lies/`` wiki into XDG role-routed dirs.

    With ``--force``, conflicting source files (destination has
    byte-mismatched content) are *quarantined* to
    ``<legacy_path>/.xdg-migration-conflicts/`` rather than dropped; the
    destination file is left untouched. Conflicts abort the migration by
    default so the caller can resolve them.
    """
    from lies.migrate import migrate_wiki

    result = migrate_wiki(legacy_path, name=name, force=force)
    if result.removed_legacy:
        typer.echo(f"migrated wiki '{name}' from {legacy_path}")
    elif result.skipped:
        typer.echo(f"wiki '{name}' already migrated (no-op)")
    typer.echo(
        f"copied={len(result.copied)} skipped={len(result.skipped)} "
        f"conflicts={len(result.conflicts)} "
        f"quarantined={len(result.quarantined)}"
    )


@app.command(name="config")
def config_cmd(
    name: str = typer.Option("default", "--name", "-n", envvar="LIES_WIKI_NAME"),
) -> None:
    """Print active model + wiki name + per-agent model assignments."""
    from lies.providers import AGENT_ROSTER, load_providers_config, resolve_model
    from lies.wiki.wiki import Wiki

    wiki = Wiki.require(name)
    typer.echo(f"wiki: {wiki.name}")

    cfg = load_providers_config(wiki.providers_path)
    if cfg is None:
        typer.echo(f"model: (no providers.toml at {wiki.providers_path})")
        typer.echo("agent models: (none configured)")
        return

    typer.echo(f"model: {cfg.default_model}")
    typer.echo("agent models:")
    width = max(len(agent_name) for agent_name in AGENT_ROSTER)
    for agent_name in AGENT_ROSTER:
        try:
            resolve_model(agent_name, cfg)
        except ProviderConfigError as exc:
            typer.echo(f"  {agent_name.ljust(width)}  (unresolved: {exc})")
            continue
        typer.echo(f"  {agent_name.ljust(width)}  {cfg.agents[agent_name]}")


@app.command()
def init(name: str) -> None:
    """Initialize a new wiki under XDG_DATA_HOME."""
    from lies.errors import WikiAlreadyExists
    from lies.wiki.layout import WikiLayout, copy_default_schema, git_init_initial
    from lies.wiki.wiki import Wiki

    wiki = Wiki(
        name=name,
        data_root=Wiki.data_root_for(name),
        config_root=xdg.config_home() / "lies" / name,
        cache_root=xdg.cache_home() / "lies" / name,
        state_root=xdg.state_home() / "lies" / name,
        runtime_root=xdg.runtime_dir_for(name),
    )
    if wiki.data_root.exists():
        typer.echo(f"error: {WikiAlreadyExists(name, wiki.data_root)}", err=True)
        raise typer.Exit(code=1)
    # All five role roots
    for root in (
        wiki.data_root,
        wiki.config_root,
        wiki.cache_root,
        wiki.state_root,
        wiki.runtime_root,
    ):
        root.mkdir(parents=True, exist_ok=True)
    # Known subdirs under config_root — ready for users to drop YAMLs in.
    wiki.collections_dir.mkdir(parents=True, exist_ok=True)
    WikiLayout(wiki.data_root).init()
    copy_default_schema(wiki.schema_path)
    git_init_initial(wiki.data_root)
    typer.echo(f"initialized wiki '{name}' at {wiki.data_root}")


@app.command()
def ingest(
    collection: str,
    *,
    source: str | None = None,
    model: str | None = None,
    name: str | None = typer.Option(None, "--name", envvar="LIES_WIKI_NAME"),
) -> None:
    """Ingest a source into a collection (creates collection if missing).

    First-time flow (LLM scraper generation) deferred to follow-up.
    Currently behaves like sync for existing collections.

    Errors if the collection YAML is missing; ``sync_helper.sync_collection``
    does not auto-scaffold a collection from a source path. Use
    ``ingest_source`` for the legacy single-source ingestion path.
    """
    from lies.etl.sync_helper import (
        acquire_heartbeat,
        release_heartbeat,
        sync_collection,
    )

    wiki = resolve_wiki(name)
    if acquire_heartbeat(wiki, wait=False, fail_busy=True) is None:
        raise typer.Exit(code=2)
    try:
        sync_collection(wiki, collection, force=False)
    finally:
        release_heartbeat(wiki)


@app.command()
def ingest_source(
    source: str = typer.Argument(..., help="Path, URL, or '-' for stdin."),
    name: str | None = typer.Option(None, "--name", envvar="LIES_WIKI_NAME"),
) -> None:
    """Atomic ingest of a single source into the wiki identified by ``name``.

    Kept for backward compatibility with the original source-path CLI
    surface (``lies ingest <source>``). Delegates to
    :meth:`Orchestrator.run_ingest`, which snapshots the working
    tree, runs the agent, and commits atomically. On any failure the
    working tree is restored and the exception is re-raised.
    """
    configure_logging()
    wiki = resolve_wiki(name)
    orch = Orchestrator(wiki)
    output = orch.run_ingest(source)
    typer.echo(output)


@app.command()
def query(
    question: str = typer.Argument(..., help="The question to ask the wiki."),
    name: str | None = typer.Option(None, "--name", envvar="LIES_WIKI_NAME"),
) -> None:
    """Query the wiki with qmd → index.md fallback."""
    configure_logging()
    wiki = resolve_wiki(name)
    orch = Orchestrator(wiki)
    # Use the host-side ``run_query`` entry point so the synthesizer with
    # qmd→index fallback runs without an LLM round-trip.
    answer = orch.run_query(question)
    console.print(Markdown(answer.answer))


@app.command()
def lint(
    name: str | None = typer.Option(None, "--name", envvar="LIES_WIKI_NAME"),
    fix: bool = typer.Option(False, "--fix", help="Apply repair plan for safe_to_fix findings."),
) -> None:
    """Run lint; with --fix also apply the repair plan."""
    configure_logging()
    wiki = resolve_wiki(name)
    orch = Orchestrator(wiki)
    # Use the host-side ``run_lint`` entry point so the lint pass writes
    # a deterministic ``wiki/lint-report.md`` and appends to ``wiki/log.md``.
    output = orch.run_lint(apply=fix)
    console.print(Markdown(output))


@app.command()
def status(
    name: str | None = typer.Option(None, "--name", envvar="LIES_WIKI_NAME"),
) -> None:
    """Show qmd status and the last few log entries."""
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


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    name: str | None = typer.Option(None, "--name", envvar="LIES_WIKI_NAME"),
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
    wiki = resolve_wiki(name)
    orch = Orchestrator(wiki)
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
                sha = atomic_commit(wiki.data_root, "manual commit")
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
    name: str | None = typer.Option(None, "--name", envvar="LIES_WIKI_NAME"),
) -> None:
    """Sync one or all collections."""
    from lies.etl.sync_helper import (
        acquire_heartbeat,
        collection_names,
        release_heartbeat,
        sync_collection,
    )

    wiki = resolve_wiki(name)
    if acquire_heartbeat(wiki, wait=wait, fail_busy=fail_busy) is None:
        raise typer.Exit(code=2)
    try:
        for coll_name in collection_names(wiki, collection):
            sync_collection(wiki, coll_name, force=force)
    finally:
        release_heartbeat(wiki)


@app.command()
def reindex(
    *,
    reconcile: bool = False,
    embed: bool = False,
    force: bool = False,
    cleanup: bool = False,
    all: bool = False,
    name: str | None = typer.Option(None, "--name", envvar="LIES_WIKI_NAME"),
) -> None:
    """Reindex QMD collections.

    ``--reconcile`` syncs each collection (running the full pipeline).
    ``--embed`` and ``--cleanup`` are currently no-op placeholders
    pending upstream qmd support; passing them prints a stderr warning
    but does not fail.
    """
    from lies.etl.sync_helper import collection_names, sync_collection

    if all:
        reconcile, embed, cleanup = True, True, True
    if force and not embed:
        raise typer.BadParameter("--force requires --embed")

    wiki = resolve_wiki(name)
    if reconcile:
        for coll_name in collection_names(wiki, None):
            sync_collection(wiki, coll_name, force=False)
    if embed:
        from lies.qmd.cli import qmd_embed

        qmd_embed(wiki.data_root, force=force)
        typer.echo(
            "warning: --embed is a no-op; upstream qmd has no embed subcommand yet.",
            err=True,
        )
    if cleanup:
        from lies.qmd.cli import qmd_cleanup

        qmd_cleanup(wiki.data_root)
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
    wiki_name: str | None = typer.Option(None, "--name", envvar="LIES_WIKI_NAME"),
) -> None:
    """Inspect, modify, and author collection configurations."""
    import json
    from datetime import datetime, timezone

    import yaml  # type: ignore[import-untyped]
    from rich.prompt import Prompt

    from lies.collections.record import (
        Collection,
        load_collection,
        save_collection,
    )

    wiki = resolve_wiki(wiki_name)
    cfg_dir = wiki.collections_dir
    if action == "list":
        for p in sorted(cfg_dir.glob("*.yaml")):
            print(p.stem)
    elif action == "show" and name:
        from lies.memory.service import WikiMemoryService

        c = load_collection(wiki, name)
        print(f"name={c.name} source={c.source} tags={c.tags}")
        # The CLI doesn't know whether sync has run in this process;
        # an empty registry means the in-process WikiMemoryService for
        # this wiki root has not registered any collection yet.
        svc = WikiMemoryService(wiki)
        registered = svc.registered_collections()
        ref = next(
            (r for r in registered if r.collection_id == name),
            None,
        )
        print(f"status: {'registered' if ref else 'pending'}")
    elif action == "modify" and name:
        raise typer.BadParameter(
            f"`lies collections modify {name}` is not implemented yet; "
            f"edit `{cfg_dir}/{name}.yaml` by hand for now."
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
        scratch_dir = wiki.scratch_dir
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
                    payload.setdefault("path", str(wiki.data_root / "raw" / name))
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
                    save_collection(wiki, collection)
                    typer.echo(f"wrote {cfg_dir / (name + '.yaml')}")
                return
            raise typer.BadParameter("agent returned unexpected output")
    else:
        raise typer.BadParameter(f"unknown action: {action}")


if __name__ == "__main__":
    app()
