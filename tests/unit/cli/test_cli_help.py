"""Parametrized tests asserting that every command's --help output contains
the help= text from the spec at
~/code/project-notes/lies/superpowers/specs/2026-08-19-cli-help-revamp-design.md
(Architecture §3 + §4).
"""

from __future__ import annotations

import re

import pytest
from typer.testing import CliRunner

from lies.cli import app

runner = CliRunner()

# Regexes that strip the leading flag spec from a rich --help option line.
# Order matters: leading flag, optional --no-flag, optional <metavar>, optional [default: ...]
_LEADING_FLAG = re.compile(r"^--[\w-]+(?:[/\s][\w-]+)*\s*")
_OPTIONAL_NO_FLAG = re.compile(r"^--[\w-]+\s*")
_OPTIONAL_METAVAR = re.compile(r"^<[^>]+>\s*")
_OPTIONAL_DEFAULT = re.compile(r"^\[[^\]]+\]\s*")


def _option_description(line: str) -> str:
    """Strip the flag spec tokens from an option line; return the remaining text.

    Empty string means the option line ended immediately after the flag spec,
    with no description text following it.
    """
    content = line.strip()
    # Strip the rich panel border characters (│) on both ends
    while content.startswith("│"):
        content = content[1:].strip()
    while content.endswith("│"):
        content = content[:-1].strip()
    content = _LEADING_FLAG.sub("", content)
    content = _OPTIONAL_NO_FLAG.sub("", content)
    content = _OPTIONAL_METAVAR.sub("", content)
    content = _OPTIONAL_DEFAULT.sub("", content)
    return content.strip()


def _option_start_lines(output: str) -> list[str]:
    """Return the lines that start an option entry in a rich --help panel.

    A line "starts an option entry" if it begins with the panel border
    character (│) followed by a flag (--...). Continuation lines
    (env-var info, wrapped descriptions) are filtered out because they
    do not start with -- directly after the panel border.
    """
    lines = output.splitlines()
    starts = []
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith(("│ --", "|--")):
            starts.append(line)
    return starts


@pytest.mark.parametrize(
    "command",
    [
        ["migrate-xdg"],
        ["ingest"],
        ["sync"],
        ["reindex"],
    ],
)
def test_every_option_has_help_text(command):
    """Every rendered option in --help output must carry a non-empty description.

    Guards against re-introducing a bare `bool = False` or `str | None = None`
    parameter without an explicit `Annotated[..., typer.Option(..., help=...)]`
    wrap (see specs/2026-08-20-bare-bool-help-gap-design.md).
    """
    result = runner.invoke(app, command + ["--help"])
    assert result.exit_code == 0, f"command {command} --help failed: {result.output}"

    for line in _option_start_lines(result.output):
        description = _option_description(line)
        assert description, f"no help text in option line for {command}: {line!r}"


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
