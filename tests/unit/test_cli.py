from __future__ import annotations

from unittest.mock import patch

from typer.testing import CliRunner

from lies import __version__
from lies.cli import app

runner = CliRunner()


def test_version_command() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_config_command_defaults() -> None:
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0
    assert "anthropic:claude-opus-4-7" in result.stdout
    assert "wiki_root" in result.stdout


def test_config_command_overrides(monkeypatch) -> None:
    monkeypatch.setenv("LIES_MODEL", "anthropic:claude-sonnet-5")
    monkeypatch.setenv("LIES_WIKI_ROOT", "/tmp/wiki")
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0
    assert "anthropic:claude-sonnet-5" in result.stdout
    assert "/tmp/wiki" in result.stdout


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
    """Anything that isn't a slash-command is forwarded to the orchestrator."""
    with patch("lies.cli.Orchestrator") as MockOrch:
        mock_instance = MockOrch.return_value
        mock_instance.run.return_value = "ok from orchestrator"
        result = runner.invoke(app, [], input="what does my wiki say about X?\n/exit\n")
    assert result.exit_code == 0
    mock_instance.run.assert_called_once_with("what does my wiki say about X?")
    assert "ok from orchestrator" in result.stdout


def test_repl_respects_wiki_root_env(monkeypatch) -> None:
    """The REPL must resolve --wiki-root from env / CLI option."""
    with patch("lies.cli.Orchestrator") as MockOrch:
        result = runner.invoke(
            app, ["--wiki-root", "/tmp/from-flag"], input="/exit\n"
        )
    assert result.exit_code == 0
    # The orchestrator must have been constructed with the resolved wiki root
    call_kwargs = MockOrch.call_args.kwargs
    assert str(call_kwargs["wiki_root"]) == "/tmp/from-flag"


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


def test_config_subcommand() -> None:
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0
    assert "model:" in result.stdout
    assert "wiki_root:" in result.stdout


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