"""Typer CLI entrypoint."""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, cast

import typer
from rich.console import Console
from rich.markdown import Markdown

from lies import __version__, xdg
from lies.constants import LIES_DATA_SUBDIR
from lies.lock_errors import WikiFlockUnrepairable, WikiLockBusy
from lies.mcp import daemon
from lies.mcp.resolution import resolve_wiki
from lies.memory.service import MAX_FLOCK_AGE_S
from lies.orchestrator import Orchestrator
from lies.providers import ProviderConfigError
from lies.providers import bootstrap as providers_bootstrap
from lies.providers import ops as providers_ops
from lies.qmd import daemon as qmd_daemon
from lies.qmd import qmd_status
from lies.scrapers.base import pick_scraper
from lies.utils.exclusive import acquire_create_lock
from lies.utils.lock_heartbeat import read_heartbeat, read_owner_pid
from lies.utils.logging import configure_logging
from lies.wiki.git import atomic_commit
from lies.wiki.layout import WikiLayout
from lies.wiki_settings import resolve_language
from lies.wikilinks import WikiLinkCorpusMissing, WikiLinkResolver

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
    _emit_missing_providers_hint(wiki.providers_path)
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
    """Print active model + wiki name + resolved language + per-agent model assignments."""
    from lies.providers import AGENT_ROSTER, load_providers_config, resolve_model

    wiki = resolve_wiki(name)
    _emit_missing_providers_hint(wiki.providers_path)
    typer.echo(f"wiki: {wiki.name}")
    typer.echo(f"language: {resolve_language(wiki)}")

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
            resolved = resolve_model(agent_name, cfg)
        except ProviderConfigError as exc:
            typer.echo(f"  {agent_name.ljust(width)}  (unresolved: {exc})")
            continue
        if isinstance(resolved, str):
            typer.echo(f"  {agent_name.ljust(width)}  {resolved}")
        else:
            # Resolved into a Model instance (custom anthropic_compatible
            # provider); show the TOML string the user wrote -- which is
            # the model identifier, not the constructed client.
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
        config_root=xdg.config_home() / LIES_DATA_SUBDIR / name,
        cache_root=xdg.cache_home() / LIES_DATA_SUBDIR / name,
        state_root=xdg.state_home() / LIES_DATA_SUBDIR / name,
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
    _emit_missing_providers_hint(wiki.providers_path)
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
    force_repair: bool = typer.Option(
        False,
        "--force-repair",
        help=(
            "Reap a stale memory flock and retry once before applying the "
            "repair plan. Only meaningful with --fix; surfaces "
            "WikiFlockUnrepairable (exit 1) if the retry still loses."
        ),
    ),
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


flock_app = typer.Typer(help="Inspect or repair a wiki's memory flock.")
app.add_typer(flock_app, name="flock")


@flock_app.callback(invoke_without_command=True)
def _flock_main(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Wiki name.")],
) -> None:
    """Capture the wiki name; subcommands dispatch via ``ctx.obj``."""
    ctx.ensure_object(dict)
    ctx.obj["name"] = name


@flock_app.command("status")
def flock_status(
    ctx: typer.Context,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON instead of text.")] = False,
) -> None:
    """Show the current memory-flock holder for the named wiki."""
    name: str = ctx.obj["name"]
    runtime_root = xdg.runtime_dir_for(name)
    lock = runtime_root / "memory.lock"
    pid_path = runtime_root / "memory.pid"
    state_path = runtime_root / "memory.state.json"
    create_lock = runtime_root / "memory.lock.create"

    if not (lock.exists() or create_lock.exists() or pid_path.exists()):
        if json_output:
            print(json.dumps({"status": "absent"}))
        else:
            typer.echo("no flock held; wiki memory is unlocked.")
        raise typer.Exit(code=2)

    pid = read_owner_pid(pid_path)
    hb = read_heartbeat(state_path)
    age_s = (time.time() - hb.started_at) if hb else None
    age_str = f"{age_s:.0f}s" if age_s is not None else "?"
    fresh = (age_s is not None) and (age_s < MAX_FLOCK_AGE_S)

    payload = {
        "wiki": name,
        "runtime": str(runtime_root),
        "pid": pid,
        "started_at": hb.started_at if hb else None,
        "scope": hb.scope if hb else "",
        "age_s": age_s,
        "limit_s": MAX_FLOCK_AGE_S,
        "fresh": fresh,
        "status": "held" if fresh else "stale",
        "files": {
            ".lock.create": create_lock.exists(),
            ".pid": pid_path.exists(),
            ".state.json": state_path.exists(),
        },
    }
    if json_output:
        print(json.dumps(payload, indent=2))
    else:
        typer.echo(f"wiki      : {payload['wiki']}")
        typer.echo(f"runtime   : {payload['runtime']}")
        typer.echo(
            f"holder    : pid={pid} started_at={hb.started_at if hb else '?'} scope={payload['scope']}"
        )
        files = payload["files"]
        typer.echo(
            f"files     : .lock.create=[{'present' if files['.lock.create'] else 'absent'}] "
            f".pid=[{pid}] .state.json=[{'ok' if files['.state.json'] else 'absent'}]"
        )
        typer.echo(
            f"age       : {age_str}  (limit={MAX_FLOCK_AGE_S}s, {'fresh' if fresh else 'stale'})"
        )
        typer.echo(f"status    : {payload['status']}")

    raise typer.Exit(code=0 if fresh else 1)


@flock_app.command("force-repair")
def flock_force_repair(ctx: typer.Context) -> None:
    """Reap the memory flock's envelope and retry; raise WikiFlockUnrepairable
    capturing the contender's pid + start time when the post-reap retry still
    loses — the flock files are gone after the reap, but the captured data
    survives long enough to cite in the operator-actionable message."""
    name: str = ctx.obj["name"]
    runtime_root = xdg.runtime_dir_for(name)
    pid_path = runtime_root / "memory.pid"
    state_path = runtime_root / "memory.state.json"
    create_lock = runtime_root / "memory.lock.create"
    lock_path = runtime_root / "memory.lock"

    # Read pid + heartbeat BEFORE the reap loop; the flock files are gone
    # after the reap and any post-retry operator-actionable message needs
    # this data to cite.
    captured_pid = read_owner_pid(pid_path)
    captured_hb = read_heartbeat(state_path)

    # First reap unconditionally (force-repair is unconditional).
    for p in (state_path, pid_path, lock_path, create_lock):
        if p.exists():
            p.unlink()
            typer.echo(f"reap     : unlinking {p.name}")

    # Retry once.
    result = acquire_create_lock(create_lock, max_age_s=MAX_FLOCK_AGE_S)
    if result is None:
        typer.echo("result   : unrepairable — manual intervention required")
        if captured_pid is not None and captured_hb is not None:
            t_iso = datetime.fromtimestamp(captured_hb.started_at, tz=UTC).isoformat()
            msg = (
                f"memory flock for wiki '{name}' held by live pid "
                f"{captured_pid} (started {t_iso}); force-repair failed "
                f"after retry. Run `lies flock {name} force-repair` to "
                f"inspect/retry or kill {captured_pid} manually."
            )
            typer.echo(msg, err=True)
            raise WikiFlockUnrepairable(msg)
        msg = (
            f"memory flock for wiki '{name}' could not be reaped; "
            f"no readable contention state. Run `lies flock {name} "
            f"force-repair` to inspect."
        )
        typer.echo(msg, err=True)
        raise WikiFlockUnrepairable(msg)
    os.close(result.fd)
    typer.echo("retry    : acquired memory.lock.create")
    typer.echo("result   : ok (recovery succeeded)")


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
        except EOFError, KeyboardInterrupt:
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
    name: str | None = typer.Option(None, "--name", envvar="LIES_WIKI_NAME"),
) -> None:
    """Reindex QMD collections.

    ``--reconcile`` syncs each collection (running the full pipeline) and
    rebuilds the in-memory wikilink corpus for downstream consumers.
    """
    from lies.etl.sync_helper import collection_names, sync_collection
    from lies.wikilinks import WikiLinkResolver

    wiki = resolve_wiki(name)
    if reconcile:
        for coll_name in collection_names(wiki, None):
            sync_collection(wiki, coll_name, force=False)
        # Spec: reindex rebuilds the corpus. No in-process consumer today
        # (YAGNI); held for the lifetime of this process.
        WikiLinkResolver.build((wiki.wiki_dir, wiki.raw_dir))


@app.command()
def collections(
    action: Annotated[str, typer.Argument()],
    name: Annotated[str | None, typer.Argument()] = None,
    *,
    tag: str | None = None,
    source: str | None = None,
    prompt: str | None = None,
    apply: bool = False,
    from_file: Path | None = typer.Option(None, "--from-file"),  # noqa: B008
    set_: list[str] | None = typer.Option(None, "--set"),  # noqa: B008
    wiki_name: str | None = typer.Option(None, "--name", envvar="LIES_WIKI_NAME"),
) -> None:
    """Inspect, modify, and author collection configurations."""
    import json
    from datetime import datetime

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
        print(f"language: {resolve_language(wiki, c)}")
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
        from dataclasses import replace as _dc_replace

        if from_file is not None and set_:
            raise typer.BadParameter("modify accepts --from-file or --set, not both")
        if from_file is None and not set_:
            raise typer.BadParameter("modify requires --from-file or --set")

        existing = load_collection(wiki, name)

        editable_top = {
            "source",
            "tags",
            "scraper_cmd",
            "doc_path",
            "mapper_model",
            "language",
            "config",
        }

        updates: dict[str, object] = {}

        if from_file is not None:
            if not from_file.exists():
                raise typer.BadParameter(f"file not found: {from_file}")
            try:
                payload = yaml.safe_load(from_file.read_text(encoding="utf-8"))
            except yaml.YAMLError as exc:
                raise typer.BadParameter(f"invalid YAML in {from_file}: {exc}") from exc
            if not isinstance(payload, dict):
                raise typer.BadParameter("--from-file root must be a mapping")
            reserved = {"name", "path", "version", "created_at", "updated_at"}
            for k in payload:
                if k in reserved:
                    raise typer.BadParameter(f"--from-file cannot set {k!r}; reserved")
                if k not in editable_top:
                    raise typer.BadParameter(
                        f"--from-file key {k!r} not editable; allowed: {sorted(editable_top)}"
                    )
            if "doc_path" in payload and payload["doc_path"] is not None:
                payload["doc_path"] = Path(payload["doc_path"])
            if "tags" in payload:
                payload["tags"] = list(payload["tags"])
            if "config" in payload and payload["config"] is None:
                payload["config"] = {}
            updates.update(payload)

        if set_:
            for raw in set_:
                if "=" not in raw:
                    raise typer.BadParameter(f"--set expects KEY=VALUE, got {raw!r}")
                key, value = raw.split("=", 1)
                key = key.strip()
                value = value.strip()
                if key.startswith("config."):
                    sub = key.split(".", 1)[1]
                    if not sub:
                        raise typer.BadParameter(f"--set {key!r}: empty subkey")
                    cfg_src = updates.get("config")
                    cfg = (
                        dict(cast("dict[str, Any]", cfg_src)) if cfg_src else dict(existing.config)
                    )
                    cfg[sub] = value
                    updates["config"] = cfg
                    continue
                if key not in editable_top:
                    raise typer.BadParameter(
                        f"key {key!r} not editable; allowed: {sorted(editable_top)}"
                    )
                if key == "tags":
                    parts = [p.strip() for p in value.split(",")]
                    parts = [p for p in parts if p]
                    if not parts:
                        raise typer.BadParameter("tags value cannot be empty")
                    updates["tags"] = parts
                elif key == "doc_path":
                    if not value:
                        raise typer.BadParameter("doc_path cannot be empty")
                    updates["doc_path"] = Path(value)
                else:
                    updates[key] = value

        updates["updated_at"] = datetime.now(tz=UTC)
        new = _dc_replace(existing, **updates)
        save_collection(wiki, new)
        typer.echo(f"updated {Collection.config_path(wiki, name)}")
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
                    now = datetime.now(tz=UTC)
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
                        payload["created_at"] = datetime.fromisoformat(created)
                    if isinstance(updated, str):
                        payload["updated_at"] = datetime.fromisoformat(updated)
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


# --- `lies providers` sub-app + first-run hint ---------------------------
#
# `providers.toml` is user-level (``$XDG_CONFIG_HOME/lies/providers.toml``),
# so the wizard + companion operators construct a ``Wiki`` manually rather
# than going through ``resolve_wiki``: the bootstrap flow cannot depend on a
# specific wiki already being initialized.

providers_app = typer.Typer(
    help="Manage the user-level providers.toml bootstrap.",
    no_args_is_help=True,
)


_HINT = (
    "providers.toml not found at {path}. "
    "Run `lies providers init` to bootstrap, or write the file by hand."
)


# Module-level seams so tests can simulate TTY / non-TTY cleanly without
# wrestling with click's CliRunner stream replacement (which always
# swaps `sys.stdout` for a BytesIO-backed `_NamedTextIOWrapper` and is
# immune to attribute patching on the C-level `isatty` method).
def _stdout_isatty() -> bool:
    """Whether stdout is attached to a TTY.

    Wrapped so tests can monkeypatch this rather than fighting the click
    stream wrapper's C-level isatty.
    """
    try:
        return sys.stdout.isatty()
    except AttributeError, ValueError:
        return False


def _emit_missing_providers_hint(path: Path) -> None:
    """Print a one-shot bootstrap pointer to stderr when providers.toml
    is missing and we're attached to a TTY.

    Skipped silently under CI / pipes so logs stay clean.
    """
    if path.exists():
        return
    if not _stdout_isatty():
        return
    typer.echo(_HINT.format(path=path), err=True)


def _providers_wiki(name: str):
    """Build a Wiki whose ``providers_path`` resolves under the live XDG
    config home. Skips ``Wiki.require`` because providers commands are
    user-level and must work before any wiki is registered."""
    from lies.wiki.wiki import Wiki

    return Wiki(
        name=name,
        data_root=Wiki.data_root_for(name),
        config_root=xdg.config_home() / LIES_DATA_SUBDIR / name,
        cache_root=xdg.cache_home() / LIES_DATA_SUBDIR / name,
        state_root=xdg.state_home() / LIES_DATA_SUBDIR / name,
        runtime_root=xdg.runtime_dir_for(name),
    )


def _cli_prompt(label: str, default: str) -> str:
    """Default interactive prompt for the wizard.

    Reads from stdin; an empty answer returns ``default``. Tests bypass
    this by injecting their own ``PromptFn`` into ``run_wizard`` directly.
    """
    suffix = f" [{default}]" if default else ""
    raw = input(f"{label}{suffix}: ")
    return raw.strip() or default


@providers_app.command("init")
def providers_init(
    *,
    check_connection: bool = typer.Option(
        False,
        "--check-connection",
        help="Ping each provider with a set API key before exiting.",
    ),
    write_env_file: Path | None = typer.Option(  # noqa: B008
        None,
        "--write-env-file",
        help="Capture API key values from current env into this file "
        "(chmod 600); only acts on providers whose env var is set.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite an existing providers.toml.",
    ),
    non_interactive: bool = typer.Option(
        False,
        "--non-interactive",
        help="Skip prompts; requires LIES_PROVIDERS_PRESET in a future release; currently raises.",
    ),
    name: str = typer.Option("default", "--name", "-n", envvar="LIES_WIKI_NAME"),
) -> None:
    """Bootstrap providers.toml through the interactive wizard."""
    wiki = _providers_wiki(name)
    target = wiki.providers_path
    if target.exists() and not force:
        typer.echo(
            f"error: {target} already exists. Use `lies providers add|"
            f"set-default|assign|unassign`, or pass --force.",
            err=True,
        )
        raise typer.Exit(code=2)
    try:
        providers_bootstrap.run_wizard(
            target,
            check_connection=check_connection,
            write_env_file=write_env_file,
            non_interactive=non_interactive,
            prompt=_cli_prompt,
        )
    except providers_bootstrap.BootstrapAborted as exc:
        typer.echo(f"aborted: {exc}", err=True)
        raise typer.Exit(code=0)


@providers_app.command("add")
def providers_add(
    name_arg: str,
    type_: str = typer.Option("anthropic", "--type", "-t"),
    api_key_env: str = typer.Option(..., "--api-key-env", "-e"),
    base_url: str | None = typer.Option(None, "--base-url", "-u"),
    name: str = typer.Option("default", "--name", "-n", envvar="LIES_WIKI_NAME"),
) -> None:
    """Append a provider entry to providers.toml."""
    from lies.providers.config import ProviderSpec
    from lies.providers.errors import ProviderConfigError

    wiki = _providers_wiki(name)
    # ``add_provider`` re-validates the type via the editor layer; cast
    # because typer cannot enforce the Literal without a custom callback.
    spec = ProviderSpec(
        name=name_arg,
        type=cast(Any, type_),
        api_key_env=api_key_env,
        base_url=base_url,
    )
    try:
        providers_ops.add_provider(wiki.providers_path, spec)
    except providers_bootstrap.ProvidersConfigMissing as exc:
        typer.echo(f"error: {exc}. Run `lies providers init` first.", err=True)
        raise typer.Exit(code=2)
    except ProviderConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2)


@providers_app.command("set-default")
def providers_set_default(
    model: str,
    name: str = typer.Option("default", "--name", "-n", envvar="LIES_WIKI_NAME"),
) -> None:
    """Replace ``default_model`` in providers.toml."""
    from lies.providers.errors import ProviderConfigError

    wiki = _providers_wiki(name)
    try:
        providers_ops.set_default_model(wiki.providers_path, model)
    except providers_bootstrap.ProvidersConfigMissing as exc:
        typer.echo(f"error: {exc}. Run `lies providers init` first.", err=True)
        raise typer.Exit(code=2)
    except ProviderConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2)


@providers_app.command("assign")
def providers_assign(
    agent: str,
    model: str,
    name: str = typer.Option("default", "--name", "-n", envvar="LIES_WIKI_NAME"),
) -> None:
    """Set ``agents[agent] = model`` in providers.toml."""
    from lies.providers.agents import AGENT_ROSTER
    from lies.providers.errors import ProviderConfigError

    if agent not in AGENT_ROSTER:
        typer.echo(
            f"error: {agent!r} not in AGENT_ROSTER; valid: {list(AGENT_ROSTER)}",
            err=True,
        )
        raise typer.Exit(code=2)
    wiki = _providers_wiki(name)
    try:
        providers_ops.assign_agent(wiki.providers_path, agent, model)
    except providers_bootstrap.ProvidersConfigMissing as exc:
        typer.echo(f"error: {exc}. Run `lies providers init` first.", err=True)
        raise typer.Exit(code=2)
    except ProviderConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2)


@providers_app.command("unassign")
def providers_unassign(
    agent: str,
    name: str = typer.Option("default", "--name", "-n", envvar="LIES_WIKI_NAME"),
) -> None:
    """Remove ``agent`` from providers.toml's [agents] table."""
    from lies.providers.agents import AGENT_ROSTER
    from lies.providers.errors import ProviderConfigError

    if agent not in AGENT_ROSTER:
        typer.echo(
            f"error: {agent!r} not in AGENT_ROSTER; valid: {list(AGENT_ROSTER)}",
            err=True,
        )
        raise typer.Exit(code=2)
    wiki = _providers_wiki(name)
    try:
        providers_ops.unassign_agent(wiki.providers_path, agent)
    except providers_bootstrap.ProvidersConfigMissing as exc:
        typer.echo(f"error: {exc}. Run `lies providers init` first.", err=True)
        raise typer.Exit(code=2)
    except ProviderConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2)


@providers_app.command("check")
def providers_check(
    name: str = typer.Option("default", "--name", "-n", envvar="LIES_WIKI_NAME"),
) -> None:
    """Probe every provider in providers.toml for connectivity."""
    from lies.providers.errors import ProviderConfigError

    wiki = _providers_wiki(name)
    try:
        status = providers_ops.check_connectivity(wiki.providers_path)
    except providers_bootstrap.ProvidersConfigMissing as exc:
        typer.echo(f"error: {exc}. Run `lies providers init` first.", err=True)
        raise typer.Exit(code=2)
    except ProviderConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2)
    width = max((len(n) for n, _, _ in status), default=0)
    for n, st, detail in status:
        typer.echo(f"  {n.ljust(width)}  {st:<8}  {detail}")


app.add_typer(providers_app, name="providers")


if __name__ == "__main__":
    app()
