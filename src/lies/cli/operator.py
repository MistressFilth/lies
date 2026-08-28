"""Operator tooling panel: mcp, flock, providers sub-apps + helpers.

Each sub-app object is created here (cheap) so ``__init__.py`` can
``app.add_typer(...)`` them. Heavy imports (mcp server, providers
modules, anthropic SDK) stay inside each command body so ``import
lies.cli`` does not pull in orchestrator or the anthropic SDK.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, cast

import typer

from lies import xdg
from lies.cli._helpers import (
    LIES_DATA_SUBDIR,
    MAX_FLOCK_AGE_S,
    WikiFlockUnrepairable,
    _emit_missing_providers_hint,
    read_heartbeat,
    read_owner_pid,
)
from lies.etl import heartbeat as _heartbeat

__all__ = (
    "_cli_prompt",
    "_providers_wiki",
    "flock_app",
    "mcp_app",
    "providers_app",
)


# ---------------------------------------------------------------------------
# mcp sub-app + commands
# ---------------------------------------------------------------------------

mcp_app = typer.Typer(
    name="mcp",
    help="Run the MCP server on stdio, or manage the http daemon.",
)


def _run_stdio() -> None:
    """Run the FastMCP server on stdio in the foreground."""
    from lies.cli._helpers import configure_logging
    from lies.mcp.server import mcp as _mcp_server

    configure_logging()
    _mcp_server.run(transport="stdio")


@mcp_app.callback(invoke_without_command=True)
def mcp_main(ctx: typer.Context) -> None:
    """Run the LIES MCP server on stdio.

    Spawn this process from any MCP-capable host (Claude Code, Cursor,
    etc.) to expose LIES tools and resources over the Model Context
    Protocol. See README "Using LIES from Claude Code" for registration
    commands.

    Bare ``lies mcp`` keeps running stdio for backward compatibility --
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
    host: str = typer.Option("127.0.0.1", "--host", help="HTTP host to bind (loopback only)."),
    port: int = typer.Option(8737, "--port", help="HTTP port to bind."),
) -> None:
    """Internal: run the MCP server on streamable-http in the foreground.

    Invoked only by ``lies mcp up`` through a re-exec. Not part of the
    supported surface; use ``up`` instead.
    """
    from lies.cli._helpers import configure_logging
    from lies.mcp import daemon
    from lies.mcp.server import mcp as _mcp_server

    try:
        daemon.require_loopback_host(host)
    except daemon.NonLoopbackBind as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    configure_logging()
    _mcp_server.run(transport="http", host=host, port=port)


@mcp_app.command()
def up(
    host: str = typer.Option("127.0.0.1", "--host", help="HTTP host to bind (loopback only)."),
    port: int = typer.Option(8737, "--port", help="HTTP port to bind."),
    timeout: float = typer.Option(10.0, help="Seconds to wait for the port to accept."),
    no_qmd: bool = typer.Option(False, "--no-qmd", help="Skip ensuring qmd's daemon."),
    name: str | None = typer.Option(
        None,
        "--name",
        envvar="LIES_WIKI_NAME",
        help="Wiki to start the daemon for (default: $LIES_WIKI_NAME).",
    ),
) -> None:
    """Start a detached streamable-http MCP daemon for this wiki.

    Also ensures qmd's own daemon is running. qmd is a search backend,
    not a prerequisite: if it cannot be started the LIES daemon still
    comes up and a single warning goes to stderr.
    """
    from lies.cli import resolve_wiki
    from lies.cli._helpers import configure_logging
    from lies.mcp import daemon
    from lies.qmd import daemon as qmd_daemon

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
    except daemon.NonLoopbackBind, daemon.PortUnavailable, daemon.DaemonBusy:
        typer.echo(f"error: {sys.exc_info()[1]}", err=True)
        raise typer.Exit(code=1)
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
    name: str | None = typer.Option(
        None,
        "--name",
        envvar="LIES_WIKI_NAME",
        help="Wiki whose daemon should be stopped (default: $LIES_WIKI_NAME).",
    ),
) -> None:
    """Stop the MCP daemon tracked for this wiki.

    Only pidfile-tracked daemons are stopped. Stdio servers spawned by an
    MCP host are never touched, and qmd's daemon is never touched at all:
    it is machine-global and shared with other wikis and tools.
    """
    from lies.cli import resolve_wiki
    from lies.cli._helpers import configure_logging
    from lies.mcp import daemon

    configure_logging()
    wiki = resolve_wiki(name)
    try:
        result = daemon.stop_daemon(wiki, grace=grace)
    except daemon.DaemonBusy, daemon.DaemonStopFailed:
        typer.echo(f"error: {sys.exc_info()[1]}", err=True)
        raise typer.Exit(code=1)
    if result.action == "none":
        typer.echo("no daemon running")
    elif result.action == "cleared_stale":
        typer.echo(f"cleared stale pidfile for pid {result.pid}")
    else:
        typer.echo(f"stopped daemon pid {result.pid} ({result.signal})")


@mcp_app.command(name="status")
def _mcp_status(
    name: str | None = typer.Option(
        None,
        "--name",
        envvar="LIES_WIKI_NAME",
        help="Wiki to report daemon status for (default: $LIES_WIKI_NAME).",
    ),
) -> None:
    """Report whether an MCP daemon is running for this wiki.

    Exit code follows the ``systemctl is-active`` convention: 0 when
    running, 1 when stopped or stale, so shell callers can branch on it.
    """
    from lies.cli import resolve_wiki
    from lies.cli._helpers import configure_logging
    from lies.mcp import daemon
    from lies.qmd import daemon as qmd_daemon

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


# ---------------------------------------------------------------------------
# flock sub-app + commands
# ---------------------------------------------------------------------------

flock_app = typer.Typer(help="Inspect or repair a wiki's memory flock.")


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
    # Tri-state liveness classification: ``"held"`` for fresh with a
    # reachable contender, ``"indeterminate"`` for fresh when ``pid_alive``
    # could not determine state (EPERM / unknown OSError), ``"stale"`` for
    # a heartbeat outside the recovery window. Indeterminate reports
    # surface ``pid_alive: indeterminate`` in the text output so operators
    # know the flock is held but the holder's state is uncertain.
    liveness: str | None = None
    if pid is not None:
        liveness = _heartbeat.pid_alive(pid)

    if fresh and liveness == "indeterminate":
        status_text = "indeterminate"
        exit_code = 0
    elif fresh:
        status_text = "held"
        exit_code = 0
    else:
        status_text = "stale"
        exit_code = 1

    payload = {
        "wiki": name,
        "runtime": str(runtime_root),
        "pid": pid,
        "started_at": hb.started_at if hb else None,
        "scope": hb.scope if hb else "",
        "age_s": age_s,
        "limit_s": MAX_FLOCK_AGE_S,
        "fresh": fresh,
        "liveness": liveness,
        "status": status_text,
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
        if liveness is not None:
            typer.echo(f"pid_alive : {liveness}")
        typer.echo(f"status    : {payload['status']}")

    raise typer.Exit(code=exit_code)


@flock_app.command("force-repair")
def flock_force_repair(ctx: typer.Context) -> None:
    """Reap the memory flock's envelope and retry; raise WikiFlockUnrepairable
    capturing the contender's pid + start time when the post-reap retry still
    loses -- the flock files are gone after the reap, but the captured data
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
    # NOTE: access via ``lies.cli.acquire_create_lock`` (attribute lookup
    # at call time) rather than an import-time binding, so tests that
    # ``monkeypatch.setattr(lies.cli, "acquire_create_lock", ...)`` take
    # effect. The other helpers used here are imported at module top
    # because they aren't patched in any test.
    import lies.cli as _cli_pkg

    result = _cli_pkg.acquire_create_lock(create_lock, max_age_s=MAX_FLOCK_AGE_S)
    if result is None:
        typer.echo("result   : unrepairable -- manual intervention required")
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


# ---------------------------------------------------------------------------
# providers sub-app + helpers + commands
# ---------------------------------------------------------------------------

providers_app = typer.Typer(
    help="Manage the user-level providers.toml bootstrap.",
    no_args_is_help=True,
)


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
    name: str = typer.Option(
        "default",
        "--name",
        "-n",
        envvar="LIES_WIKI_NAME",
        help="Wiki context for providers.toml resolution (default: $LIES_WIKI_NAME, falls back to 'default').",
    ),
) -> None:
    """Bootstrap providers.toml through the interactive wizard."""
    from lies.providers import bootstrap as providers_bootstrap

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
    name_arg: Annotated[str, typer.Argument(help="Provider id (the [providers.<id>] table key).")],
    type_: str = typer.Option(
        "anthropic",
        "--type",
        "-t",
        help="Provider type (anthropic, openai, anthropic_compatible, ollama).",
    ),
    api_key_env: str = typer.Option(
        ...,
        "--api-key-env",
        "-e",
        help="Name of the environment variable holding the provider's API key.",
    ),
    base_url: str | None = typer.Option(
        None,
        "--base-url",
        "-u",
        help="Base URL for the provider's API (anthropic_compatible providers only).",
    ),
    name: str = typer.Option(
        "default",
        "--name",
        "-n",
        envvar="LIES_WIKI_NAME",
        help="Wiki context for providers.toml resolution (default: $LIES_WIKI_NAME, falls back to 'default').",
    ),
) -> None:
    """Append a provider entry to providers.toml."""
    from lies.providers import bootstrap as providers_bootstrap
    from lies.providers import ops as providers_ops
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
    model: Annotated[
        str, typer.Argument(help="Model string to set as default (e.g. 'minimax:claude-opus').")
    ],
    name: str = typer.Option(
        "default",
        "--name",
        "-n",
        envvar="LIES_WIKI_NAME",
        help="Wiki context for providers.toml resolution (default: $LIES_WIKI_NAME, falls back to 'default').",
    ),
) -> None:
    """Replace ``default_model`` in providers.toml."""
    from lies.providers import bootstrap as providers_bootstrap
    from lies.providers import ops as providers_ops
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
    agent: Annotated[
        str, typer.Argument(help="Agent name (must be one of the LIES AGENT_ROSTER).")
    ],
    model: Annotated[
        str,
        typer.Argument(help="Model string to assign to the agent (e.g. 'minimax:claude-opus')."),
    ],
    name: str = typer.Option(
        "default",
        "--name",
        "-n",
        envvar="LIES_WIKI_NAME",
        help="Wiki context for providers.toml resolution (default: $LIES_WIKI_NAME, falls back to 'default').",
    ),
) -> None:
    """Set ``agents[agent] = model`` in providers.toml."""
    from lies.providers import bootstrap as providers_bootstrap
    from lies.providers import ops as providers_ops
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
    agent: Annotated[
        str, typer.Argument(help="Agent name to remove (must be one of the LIES AGENT_ROSTER).")
    ],
    name: str = typer.Option(
        "default",
        "--name",
        "-n",
        envvar="LIES_WIKI_NAME",
        help="Wiki context for providers.toml resolution (default: $LIES_WIKI_NAME, falls back to 'default').",
    ),
) -> None:
    """Remove ``agent`` from providers.toml's [agents] table."""
    from lies.providers import bootstrap as providers_bootstrap
    from lies.providers import ops as providers_ops
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
    name: str = typer.Option(
        "default",
        "--name",
        "-n",
        envvar="LIES_WIKI_NAME",
        help="Wiki context for providers.toml resolution (default: $LIES_WIKI_NAME, falls back to 'default').",
    ),
) -> None:
    """Probe every provider in providers.toml for connectivity."""
    from lies.providers import bootstrap as providers_bootstrap
    from lies.providers import ops as providers_ops
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


@providers_app.command("list")
def providers_list(
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Emit a JSON array of {id, default_model, agents} per provider instead of a table.",
        ),
    ] = False,
    name: str = typer.Option(
        "default",
        "--name",
        "-n",
        envvar="LIES_WIKI_NAME",
        help="Wiki context for providers.toml resolution (default: $LIES_WIKI_NAME, falls back to 'default').",
    ),
) -> None:
    """List every provider configured in providers.toml with default model and agent assignments."""
    import tomllib

    wiki = _providers_wiki(name)
    path = wiki.providers_path
    if not path.exists():
        typer.echo(
            f"error: no providers.toml at {path}. Run `lies providers init` first.",
            err=True,
        )
        raise typer.Exit(code=2)
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        typer.echo(f"error: failed to parse {path}: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    raw_providers = data.get("providers") or {}
    agents = data.get("agents") or {}
    default_model = data.get("default_model", "")
    providers: dict[str, dict[str, object]] = {
        pid: body for pid, body in raw_providers.items() if isinstance(body, dict)
    }

    if json_output:
        typer.echo(
            json.dumps(
                [
                    {
                        "id": pid,
                        "default_model": default_model,
                        "agents": agents,
                    }
                    for pid in providers
                ],
                indent=2,
            )
        )
        return

    from rich.console import Console
    from rich.table import Table

    table = Table(title="providers")
    table.add_column("id")
    table.add_column("default_model")
    table.add_column("agents")
    agent_summary = ", ".join(f"{a}={m}" for a, m in agents.items()) or "(default agent)"
    for pid in providers:
        table.add_row(pid, str(default_model), agent_summary)
    Console().print(table)
