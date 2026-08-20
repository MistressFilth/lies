"""Parametrized tests asserting that every command's --help output contains
the help= text from the spec at
~/code/project-notes/lies/superpowers/specs/2026-08-19-cli-help-revamp-design.md
(Architecture §3 + §4).
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from lies.cli import app

runner = CliRunner()


@pytest.mark.parametrize(
    "command,expected_substrings",
    [
        # Top-level commands
        (["version"], ["Print the LIES version"]),
        (["migrate-xdg"], ["Migrate a legacy ``.lies/``"]),
        (["config"], ["Print active model"]),
        (["init"], ["Initialize a new wiki"]),
        (["ingest"], ["Ingest a source"]),
        (["ingest-source"], ["Atomic ingest"]),
        (["query"], ["Query the wiki"]),
        (["lint"], ["Run lint"]),
        (["status"], ["Show qmd status"]),
        (["sync"], ["Sync one or all"]),
        (["reindex"], ["Reindex QMD"]),
        (["collections"], ["Inspect, modify, and author collection"]),
        (["mcp"], ["Run the MCP server"]),
        (["flock"], ["Inspect or repair"]),
        (["providers"], ["Manage the user-level providers.toml"]),
        # collections subcommands
        (["collections", "list"], ["List every collection"]),
        (["collections", "show", "mywiki"], ["Show a single collection"]),
        (["collections", "new"], ["Create a new collection"]),
        (["collections", "modify", "mywiki"], ["Mutate an existing collection"]),
        (["collections", "delete", "mywiki"], ["Delete a collection"]),
        # providers subcommands (existing + new)
        (["providers", "init"], ["Bootstrap providers.toml"]),
        (["providers", "add"], ["Append a provider"]),
        (["providers", "set-default"], ["default_model"]),
        (["providers", "assign"], ["agents"]),
        (["providers", "unassign"], ["agent"]),
        (["providers", "check"], ["Probe every provider"]),
        (["providers", "list"], ["List every provider"]),
        # flock subcommands (wiki name is a parent-level typer.Argument,
        # so the syntax is ``flock <name> <subcommand>``, not the order the
        # brief sketched).
        (["flock", "mywiki", "status"], ["Show the current memory-flock"]),
        (["flock", "mywiki", "force-repair"], ["Reap the memory flock"]),
        # mcp subcommands
        (["mcp", "start"], ["Run the MCP server on stdio"]),
        (["mcp", "up"], ["Start a detached streamable-http"]),
        (["mcp", "down"], ["Stop the MCP daemon"]),
        (["mcp", "status"], ["Report whether an MCP daemon"]),
    ],
)
def test_command_help_contains_help_text(command, expected_substrings):
    result = runner.invoke(app, command + ["--help"])
    assert result.exit_code == 0, f"command {command} --help failed: {result.output}"
    for substring in expected_substrings:
        assert substring in result.output, (
            f"{substring!r} not in {command} --help output:\n{result.output}"
        )
