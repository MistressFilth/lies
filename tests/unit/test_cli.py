from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from lies import __version__, xdg
from lies.cli import app
from lies.collections.record import Collection, load_collection, save_collection
from lies.wiki.wiki import Wiki

runner = CliRunner()


def test_version_command() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_config_command_defaults() -> None:
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0
    assert "wiki: default" in result.stdout
    assert "(no providers.toml" in result.stdout
    assert "agent models: (none configured)" in result.stdout


def test_config_command_overrides(monkeypatch, tmp_path) -> None:
    Wiki.data_root_for("wiki").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("LIES_WIKI_NAME", "wiki")
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0
    assert "wiki: wiki" in result.stdout
    assert "(no providers.toml" in result.stdout


# Smoke tests for the REPL (`lies` with no subcommand).
#
# These tests verify the REPL is reachable: with no subcommand, the
# callback's REPL runs (not the help screen). The orchestrator is
# patched so the smoke tests do not require a real LLM/ANTHROPIC_API_KEY.


def test_repl_runs_with_no_subcommand() -> None:
    """`lies` (no args) must enter the REPL, not print help."""
    with patch("lies.cli.Orchestrator"):
        # /exit quits the REPL cleanly
        result = runner.invoke(app, [], input="/exit\n")
    assert result.exit_code == 0
    # The REPL banner and prompt must be present
    assert "LIES REPL" in result.stdout
    assert "lies>" in result.stdout
    # Help text would only appear if `no_args_is_help` were still on
    assert "Usage:" not in result.stdout


def test_repl_quit_alias() -> None:
    """`/quit` is an alias for `/exit`."""
    with patch("lies.cli.Orchestrator"):
        result = runner.invoke(app, [], input="/quit\n")
    assert result.exit_code == 0
    assert "LIES REPL" in result.stdout
    assert "bye." in result.stdout


def test_repl_help_command() -> None:
    """`/help` should list REPL commands without dispatching to the orchestrator."""
    with patch("lies.cli.Orchestrator") as MockOrch:
        result = runner.invoke(app, [], input="/help\n/exit\n")
    assert result.exit_code == 0
    assert "/ingest" in result.stdout
    assert "/query" in result.stdout
    assert "/lint" in result.stdout
    # /help must NOT invoke the orchestrator
    MockOrch.return_value.run.assert_not_called()


def test_repl_eof_exits_cleanly() -> None:
    """Reaching EOF (empty stdin) must exit the REPL with code 0."""
    with patch("lies.cli.Orchestrator"):
        result = runner.invoke(app, [], input="")
    assert result.exit_code == 0
    assert "LIES REPL" in result.stdout
    assert "bye." in result.stdout


def test_repl_dispatches_free_form_to_orchestrator() -> None:
    """Free-form commands use invisible memory by default."""
    with patch("lies.cli.Orchestrator") as MockOrch:
        mock_instance = MockOrch.return_value
        mock_instance.run_with_memory.return_value = "ok from orchestrator"
        result = runner.invoke(app, [], input="what does my wiki say about X?\n/exit\n")
    assert result.exit_code == 0
    mock_instance.run_with_memory.assert_called_once_with("what does my wiki say about X?")
    mock_instance.run.assert_not_called()
    assert "ok from orchestrator" in result.stdout


def test_repl_no_memory_uses_plain_orchestrator_run() -> None:
    with patch("lies.cli.Orchestrator") as MockOrch:
        mock_instance = MockOrch.return_value
        mock_instance.run.return_value = "plain"
        result = runner.invoke(app, ["--no-memory"], input="hello\n/exit\n")
    assert result.exit_code == 0
    mock_instance.run.assert_called_once_with("hello")
    mock_instance.run_with_memory.assert_not_called()


def test_repl_respects_wiki_name_env(monkeypatch) -> None:
    """The REPL must resolve `--name` from the CLI flag and pass a Wiki to the orchestrator."""
    name = "from-flag"
    monkeypatch.setenv("LIES_WIKI_NAME", name)
    Wiki.data_root_for(name).mkdir(parents=True, exist_ok=True)
    with patch("lies.cli.Orchestrator") as MockOrch:
        result = runner.invoke(app, ["--name", name], input="/exit\n")
    assert result.exit_code == 0
    # The REPL constructs the orchestrator positionally: Orchestrator(wiki).
    positional, _ = MockOrch.call_args
    assert positional[0].name == name


def test_repl_ignores_blank_lines() -> None:
    """Blank lines must not be dispatched and must not crash the REPL."""
    with patch("lies.cli.Orchestrator") as MockOrch:
        result = runner.invoke(app, [], input="\n\n   \n/exit\n")
    assert result.exit_code == 0
    MockOrch.return_value.run.assert_not_called()


# Tests for the `mcp` subcommand (Task 5).
#
# These verify that `lies mcp` is a registered Typer subcommand that
# delegates to FastMCP's ``mcp.run(transport="stdio")``. The wiring is
# exercised by monkeypatching the imported ``mcp`` instance's ``run``
# method, asserting it is invoked exactly once with the expected kwargs.
# No real stdio MCP server is spawned inside the test process.


def test_version_subcommand() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.startswith("lies ")


def test_mcp_subcommand_is_registered() -> None:
    """`lies mcp` is a registered subcommand (does not crash with 'no such command')."""
    result = runner.invoke(app, ["mcp", "--help"])
    assert result.exit_code == 0
    assert "mcp" in result.stdout.lower() or "stdio" in result.stdout.lower()


def test_mcp_subcommand_invokes_fastmcp_run(monkeypatch) -> None:
    """`lies mcp` calls mcp.run(transport='stdio') exactly once."""
    from lies.mcp.server import mcp as _mcp

    calls: list[dict] = []
    monkeypatch.setattr(_mcp, "run", lambda **kwargs: calls.append(kwargs))
    result = runner.invoke(app, ["mcp"])
    assert result.exit_code == 0
    assert calls == [{"transport": "stdio"}]


# Daemon lifecycle subcommands. `lies mcp` (bare) and `lies mcp start`
# must both keep running stdio: every registered MCP host invokes the
# bare form, so breaking it breaks live sessions.


@pytest.fixture(autouse=True)
def _stub_qmd(monkeypatch):
    """Keep `up` and `status` from shelling out to the real qmd.

    Without this every CLI test would depend on whether a qmd daemon
    happens to be running on the developer's machine.
    """
    from lies.qmd import daemon as qmd_daemon

    state = qmd_daemon.QmdState(
        installed=True, running=True, pid=1234, detail="qmd daemon running (pid 1234)"
    )
    monkeypatch.setattr(qmd_daemon, "ensure_qmd_daemon", lambda **kwargs: state)
    monkeypatch.setattr(qmd_daemon, "qmd_daemon_state", lambda: state)
    # Pre-register the default wiki so REPL tests don't trip WikiNotRegistered.
    Wiki.data_root_for("default").mkdir(parents=True, exist_ok=True)
    return state


def _register_wiki(name: str) -> None:
    """Pre-create a wiki's data_root so `Wiki.require(name)` succeeds."""
    Wiki.data_root_for(name).mkdir(parents=True, exist_ok=True)


def test_up_ensures_the_qmd_daemon(monkeypatch, tmp_path: Path) -> None:
    from datetime import datetime, timezone

    from lies.mcp import daemon
    from lies.qmd import daemon as qmd_daemon

    name = "cli-up-qmd"
    _register_wiki(name)
    rec = daemon.PidRecord(
        pid=1,
        host="127.0.0.1",
        port=8737,
        transport="http",
        started_at=datetime.now(timezone.utc),
        wiki_root=str(tmp_path),
        version="0.5.0",
    )
    monkeypatch.setattr(daemon, "spawn_daemon", lambda *a, **k: rec)
    calls: list[int] = []
    monkeypatch.setattr(
        qmd_daemon,
        "ensure_qmd_daemon",
        lambda **k: (
            calls.append(1) or qmd_daemon.QmdState(True, True, 42, "qmd daemon running (pid 42)")
        ),
    )
    result = runner.invoke(app, ["mcp", "up", "--name", name])
    assert result.exit_code == 0
    assert calls == [1]


def test_up_skips_qmd_with_no_qmd_flag(monkeypatch, tmp_path: Path) -> None:
    from datetime import datetime, timezone

    from lies.mcp import daemon
    from lies.qmd import daemon as qmd_daemon

    name = "cli-up-no-qmd"
    _register_wiki(name)
    rec = daemon.PidRecord(
        pid=1,
        host="127.0.0.1",
        port=8737,
        transport="http",
        started_at=datetime.now(timezone.utc),
        wiki_root=str(tmp_path),
        version="0.5.0",
    )
    monkeypatch.setattr(daemon, "spawn_daemon", lambda *a, **k: rec)
    calls: list[int] = []
    monkeypatch.setattr(qmd_daemon, "ensure_qmd_daemon", lambda **k: calls.append(1))
    result = runner.invoke(app, ["mcp", "up", "--no-qmd", "--name", name])
    assert result.exit_code == 0
    assert calls == []


def test_up_succeeds_when_qmd_is_unavailable(monkeypatch, tmp_path: Path) -> None:
    """qmd is a search backend, not a prerequisite."""
    from datetime import datetime, timezone

    from lies.mcp import daemon
    from lies.qmd import daemon as qmd_daemon

    name = "cli-up-no-qmd-runtime"
    _register_wiki(name)
    rec = daemon.PidRecord(
        pid=1,
        host="127.0.0.1",
        port=8737,
        transport="http",
        started_at=datetime.now(timezone.utc),
        wiki_root=str(tmp_path),
        version="0.5.0",
    )
    monkeypatch.setattr(daemon, "spawn_daemon", lambda *a, **k: rec)
    monkeypatch.setattr(
        qmd_daemon,
        "ensure_qmd_daemon",
        lambda **k: qmd_daemon.QmdState(False, False, None, "qmd is not installed"),
    )
    result = runner.invoke(app, ["mcp", "up", "--name", name])
    assert result.exit_code == 0
    combined = result.output + (result.stderr if result.stderr_bytes else "")
    assert "not installed" in combined


def test_down_never_touches_qmd(monkeypatch, tmp_path: Path) -> None:
    """The one behavior protecting a shared, machine-global resource."""
    from lies.mcp import daemon
    from lies.qmd import daemon as qmd_daemon

    name = "cli-down"
    _register_wiki(name)
    monkeypatch.setattr(
        daemon,
        "stop_daemon",
        lambda *a, **k: daemon.StopResult(action="stopped", pid=9, signal="SIGTERM"),
    )

    def _explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("down must never invoke qmd lifecycle functions")

    monkeypatch.setattr(qmd_daemon, "ensure_qmd_daemon", _explode)
    result = runner.invoke(app, ["mcp", "down", "--name", name])
    assert result.exit_code == 0


def test_mcp_start_invokes_fastmcp_stdio(monkeypatch) -> None:
    from lies.mcp.server import mcp as _mcp

    calls: list[dict] = []
    monkeypatch.setattr(_mcp, "run", lambda **kwargs: calls.append(kwargs))
    result = runner.invoke(app, ["mcp", "start"])
    assert result.exit_code == 0
    assert calls == [{"transport": "stdio"}]


def test_serve_is_hidden_from_help() -> None:
    result = runner.invoke(app, ["mcp", "--help"])
    assert result.exit_code == 0
    assert "_serve" not in result.stdout
    assert "up" in result.stdout
    assert "down" in result.stdout
    assert "status" in result.stdout


def test_serve_runs_http_transport(monkeypatch) -> None:
    from lies.mcp.server import mcp as _mcp

    calls: list[dict] = []
    monkeypatch.setattr(_mcp, "run", lambda **kwargs: calls.append(kwargs))
    result = runner.invoke(app, ["mcp", "_serve", "--host", "127.0.0.1", "--port", "9100"])
    assert result.exit_code == 0
    assert calls == [{"transport": "http", "host": "127.0.0.1", "port": 9100}]


def test_serve_rejects_non_loopback_host(monkeypatch) -> None:
    from lies.mcp.server import mcp as _mcp

    calls: list[dict] = []
    monkeypatch.setattr(_mcp, "run", lambda **kwargs: calls.append(kwargs))

    result = runner.invoke(app, ["mcp", "_serve", "--host", "0.0.0.0", "--port", "9100"])

    assert result.exit_code == 1
    assert "has no authentication" in result.stderr
    assert calls == []


def test_up_prints_url_on_success(monkeypatch, tmp_path: Path) -> None:
    from datetime import datetime, timezone

    from lies.mcp import daemon

    name = "cli-up-print-url"
    _register_wiki(name)
    rec = daemon.PidRecord(
        pid=4242,
        host="127.0.0.1",
        port=8737,
        transport="http",
        started_at=datetime.now(timezone.utc),
        wiki_root=str(tmp_path),
        version="0.5.0",
    )
    monkeypatch.setattr(daemon, "spawn_daemon", lambda *a, **k: rec)
    result = runner.invoke(app, ["mcp", "up", "--name", name])
    assert result.exit_code == 0
    assert "4242" in result.stdout
    assert daemon.MCP_PATH in result.stdout


def test_up_is_idempotent_when_already_running(monkeypatch, tmp_path: Path) -> None:
    from datetime import datetime, timezone

    from lies.mcp import daemon

    name = "cli-up-already"
    _register_wiki(name)
    rec = daemon.PidRecord(
        pid=7,
        host="127.0.0.1",
        port=8737,
        transport="http",
        started_at=datetime.now(timezone.utc),
        wiki_root=str(tmp_path),
        version="0.5.0",
    )

    message = f"lies mcp daemon already running at {daemon.daemon_url(rec)} (pid {rec.pid})"

    def _raise(*args: object, **kwargs: object) -> None:
        raise daemon.DaemonAlreadyRunning(message, record=rec)

    monkeypatch.setattr(daemon, "spawn_daemon", _raise)
    result = runner.invoke(app, ["mcp", "up", "--name", name])
    assert result.exit_code == 0
    assert result.stdout.startswith("lies mcp daemon")
    assert "already running" in result.stdout


def test_up_exits_1_on_non_loopback_host(monkeypatch, tmp_path: Path) -> None:
    from lies.mcp import daemon

    name = "cli-up-non-loopback"
    _register_wiki(name)
    calls: list[str] = []

    def _raise(*args: object, **kwargs: object) -> None:
        calls.append("spawn")
        raise daemon.NonLoopbackBind("refusing host '0.0.0.0': daemon has no authentication")

    monkeypatch.setattr(daemon, "spawn_daemon", _raise)

    result = runner.invoke(app, ["mcp", "up", "--host", "0.0.0.0", "--name", name])

    assert result.exit_code == 1
    assert "has no authentication" in result.stderr
    assert calls == ["spawn"]


def test_up_exits_1_on_port_conflict(monkeypatch, tmp_path: Path) -> None:
    from lies.mcp import daemon

    name = "cli-up-port"
    _register_wiki(name)

    def _raise(*args: object, **kwargs: object) -> None:
        raise daemon.PortUnavailable("127.0.0.1:8737 is already in use")

    monkeypatch.setattr(daemon, "spawn_daemon", _raise)
    result = runner.invoke(app, ["mcp", "up", "--name", name])
    assert result.exit_code == 1


def test_up_tails_the_log_on_start_failure(monkeypatch, tmp_path: Path) -> None:
    from lies.mcp import daemon

    name = "cli-up-tail-log"
    _register_wiki(name)

    def _raise(*args: object, **kwargs: object) -> None:
        raise daemon.DaemonStartFailed("daemon exited with code 3")

    monkeypatch.setattr(daemon, "spawn_daemon", _raise)
    monkeypatch.setattr(daemon, "tail_log", lambda *a, **k: ["ImportError: boom"])
    result = runner.invoke(app, ["mcp", "up", "--name", name])
    assert result.exit_code == 1
    # Click 8.2 split stderr out of `.output`; tolerate either arrangement.
    combined = result.output + (result.stderr if result.stderr_bytes else "")
    assert "ImportError: boom" in combined


def test_down_exits_0_when_nothing_running(monkeypatch, tmp_path: Path) -> None:
    from lies.mcp import daemon

    name = "cli-down-nothing"
    _register_wiki(name)
    monkeypatch.setattr(
        daemon,
        "stop_daemon",
        lambda *a, **k: daemon.StopResult(action="none", pid=None, signal=None),
    )
    result = runner.invoke(app, ["mcp", "down", "--name", name])
    assert result.exit_code == 0
    assert "no daemon running" in result.stdout


def test_down_reports_the_signal(monkeypatch, tmp_path: Path) -> None:
    from lies.mcp import daemon

    name = "cli-down-signal"
    _register_wiki(name)
    monkeypatch.setattr(
        daemon,
        "stop_daemon",
        lambda *a, **k: daemon.StopResult(action="stopped", pid=99, signal="SIGTERM"),
    )
    result = runner.invoke(app, ["mcp", "down", "--name", name])
    assert result.exit_code == 0
    assert "99" in result.stdout
    assert "SIGTERM" in result.stdout


def test_status_exits_1_when_stopped(monkeypatch, tmp_path: Path) -> None:
    from lies.mcp import daemon

    name = "cli-status-stopped"
    _register_wiki(name)

    def _status(_wiki):
        return daemon.StatusResult(
            running=False,
            record=None,
            stale=False,
            url=None,
            uptime_s=None,
            log=tmp_path / ".lies" / "mcp.log",
        )

    monkeypatch.setattr(daemon, "daemon_status", _status)
    result = runner.invoke(app, ["mcp", "status", "--name", name])
    assert result.exit_code == 1
    assert "stopped" in result.stdout


def test_status_exits_0_when_running(monkeypatch, tmp_path: Path) -> None:
    from datetime import datetime, timezone

    from lies.mcp import daemon

    name = "cli-status-running"
    _register_wiki(name)
    rec = daemon.PidRecord(
        pid=55,
        host="127.0.0.1",
        port=8737,
        transport="http",
        started_at=datetime.now(timezone.utc),
        wiki_root=str(tmp_path),
        version="0.5.0",
    )

    def _status(_wiki):
        return daemon.StatusResult(
            running=True,
            record=rec,
            stale=False,
            url=daemon.daemon_url(rec),
            uptime_s=12.5,
            log=tmp_path / ".lies" / "mcp.log",
        )

    monkeypatch.setattr(daemon, "daemon_status", _status)
    result = runner.invoke(app, ["mcp", "status", "--name", name])
    assert result.exit_code == 0
    assert "running" in result.stdout
    assert "55" in result.stdout


# Tests for `lies collections modify` (Task 3).
#
# The `modify` subcommand accepts either `--from-file PATH` (a YAML
# patch) or `--set KEY=VALUE` (one or more). Both flags are mutually
# exclusive; at least one is required. The fixture mirrors the pattern
# in `tests/unit/test_cli_collections_show.py`: it sets
# `LIES_WIKI_NAME` and the LIES-specific XDG overrides so the test's
# `_seed_collection` and the CLI's `resolve_wiki` agree on the same
# wiki root under `tmp_path`.


def _combined_output(result) -> str:
    """Click 8.2+ splits stderr from `.output`; tolerate either layout."""
    return result.output + (result.stderr if result.stderr_bytes else "")


@pytest.fixture
def wiki(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Wiki:
    name = "modify"
    monkeypatch.setenv("LIES_WIKI_NAME", name)
    monkeypatch.setenv("LIES_XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("LIES_XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("LIES_XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("LIES_XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("LIES_XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    wiki = Wiki(
        name=name,
        data_root=xdg.data_home() / "lies" / name,
        config_root=xdg.config_home() / "lies" / name,
        cache_root=xdg.cache_home() / "lies" / name,
        state_root=xdg.state_home() / "lies" / name,
        runtime_root=xdg.runtime_dir_for(name),
    )
    wiki.data_root.mkdir(parents=True, exist_ok=True)
    wiki.collections_dir.mkdir(parents=True, exist_ok=True)
    return wiki


def _seed_collection(wiki_obj: Wiki, name: str) -> Collection:
    save_collection(
        wiki_obj,
        Collection(
            name=name,
            path=PurePosixPath(f"/raw/{name}"),
            source="https://old.example.com",
            tags=["old"],
            scraper_cmd=None,
            doc_path=None,
            mapper_model=None,
            language=None,
            version="1",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            config={},
        ),
    )
    return load_collection(wiki_obj, name)


def test_modify_set_tags_comma_split(wiki: Wiki) -> None:
    _seed_collection(wiki, "alpha")
    result = runner.invoke(
        app,
        ["collections", "modify", "alpha", "--set", "tags=stdlib,core"],
    )
    assert result.exit_code == 0, result.output
    loaded = load_collection(wiki, "alpha")
    assert loaded.tags == ["stdlib", "core"]


def test_modify_set_config_dotted(wiki: Wiki) -> None:
    _seed_collection(wiki, "alpha")
    result = runner.invoke(
        app,
        ["collections", "modify", "alpha", "--set", "config.render_cmd=foo"],
    )
    assert result.exit_code == 0, result.output
    loaded = load_collection(wiki, "alpha")
    assert loaded.config == {"render_cmd": "foo"}


def test_modify_set_rejects_unknown_key(wiki: Wiki) -> None:
    _seed_collection(wiki, "alpha")
    result = runner.invoke(
        app,
        ["collections", "modify", "alpha", "--set", "path=foo"],
    )
    assert result.exit_code != 0
    assert "not editable" in _combined_output(result)


def test_modify_set_and_from_file_conflict(wiki: Wiki, tmp_path: Path) -> None:
    _seed_collection(wiki, "alpha")
    patch = tmp_path / "p.yaml"
    patch.write_text("tags: []\n", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "collections",
            "modify",
            "alpha",
            "--from-file",
            str(patch),
            "--set",
            "tags=foo",
        ],
    )
    assert result.exit_code != 0
    assert "not both" in _combined_output(result)


def test_modify_no_flags_errors(wiki: Wiki) -> None:
    _seed_collection(wiki, "alpha")
    result = runner.invoke(app, ["collections", "modify", "alpha"])
    assert result.exit_code != 0
    assert "requires" in _combined_output(result)


def test_modify_from_file_applies_editable_fields(wiki: Wiki, tmp_path: Path) -> None:
    _seed_collection(wiki, "alpha")
    patch = tmp_path / "p.yaml"
    patch.write_text(
        "tags: [stdlib, core]\nlanguage: en\nconfig:\n  render_cmd: foo\n",
        encoding="utf-8",
    )
    result = runner.invoke(
        app,
        ["collections", "modify", "alpha", "--from-file", str(patch)],
    )
    assert result.exit_code == 0, result.output
    loaded = load_collection(wiki, "alpha")
    assert loaded.tags == ["stdlib", "core"]
    assert loaded.language == "en"
    assert loaded.config == {"render_cmd": "foo"}
    assert loaded.created_at == datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_modify_from_file_rejects_name_field(wiki: Wiki, tmp_path: Path) -> None:
    _seed_collection(wiki, "alpha")
    patch = tmp_path / "p.yaml"
    patch.write_text("name: other\n", encoding="utf-8")
    result = runner.invoke(
        app,
        ["collections", "modify", "alpha", "--from-file", str(patch)],
    )
    assert result.exit_code != 0
    assert "name" in _combined_output(result)


def test_modify_from_file_rejects_invalid_yaml(wiki: Wiki, tmp_path: Path) -> None:
    _seed_collection(wiki, "alpha")
    patch = tmp_path / "p.yaml"
    patch.write_text("not: a: mapping: at: all", encoding="utf-8")
    result = runner.invoke(
        app,
        ["collections", "modify", "alpha", "--from-file", str(patch)],
    )
    assert result.exit_code != 0
    combined = _combined_output(result)
    assert "invalid YAML" in combined or "mapping" in combined


def test_modify_from_file_not_found(wiki: Wiki, tmp_path: Path) -> None:
    _seed_collection(wiki, "alpha")
    patch = tmp_path / "missing.yaml"
    result = runner.invoke(
        app,
        ["collections", "modify", "alpha", "--from-file", str(patch)],
    )
    assert result.exit_code != 0
    assert "not found" in _combined_output(result)
