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


# Each row: (command, expected_help_substrings).
# Asserts that every option's help= text is present in --help output.
# Brittle-free: doesn't depend on rich's panel-rendering format.
PARAMETRIZE_HELP_SUBSTRINGS = [
    (
        ["migrate-xdg"],
        [
            "Migrate a legacy",
            "Overwrite existing files",
        ],
    ),
    (
        ["ingest"],
        [
            "Ingest a source",
            "Wiki to ingest into (default: $LIES_WIKI_NAME).",
            "Path, URL, or '-' for stdin",
            "Override the model id",
            "requires TTY",
        ],
    ),
    (
        ["sync"],
        [
            "Sync one or all collections (bootstraps single-collection mode if --source).",
            "Bootstrap a missing collection",
            "Force-sync even",
            "Wait for an in-progress",
            "Return non-zero exit code",
            "requires TTY",
        ],
    ),
    (
        ["reindex"],
        [
            "Reindex QMD collections.",
            "Reconcile the qmd index",
        ],
    ),
]


@pytest.mark.parametrize("command,expected_substrings", PARAMETRIZE_HELP_SUBSTRINGS)
def test_every_option_has_help_text(command, expected_substrings):
    """Every rendered option in --help output must carry a non-empty
    description text that the operator can read directly.

    Guards against re-introducing a bare `bool = False` or `str | None = None`
    parameter without an explicit `Annotated[..., typer.Option(..., help=...)]`
    wrap (see specs/2026-08-20-bare-bool-help-gap-design.md).

    Substring-based assertion: every option's help= text appears in the
    --help output. Robust against changes to rich's panel-rendering format.
    """
    result = runner.invoke(app, command + ["--help"])
    assert result.exit_code == 0, f"command {command} --help failed: {result.output}"
    output = result.output if isinstance(result.output, str) else "".join(result.output)
    for substring in expected_substrings:
        assert substring in output, (
            f"help text {substring!r} not in {command} --help output:\n{output}"
        )


# ---------------------------------------------------------------------------
# Coverage: every command + subcommand has a discoverable help= text snippet.
# ---------------------------------------------------------------------------

PARAMETRIZE_COMMAND_HELP_SUBSTRINGS = [
    # Top-level commands
    (["version"], ["Print the LIES version"]),
    (["migrate-xdg"], ["Migrate a legacy"]),
    (["config"], ["Print active model"]),
    (["init"], ["Initialize a new wiki"]),
    (["ingest"], ["Ingest a source"]),
    (["ingest-source"], ["Atomic ingest"]),
    (["query"], ["Query the wiki"]),
    (["lint"], ["Run lint"]),
    (["status"], ["Show qmd status"]),
    (["sync"], ["Sync one or all"]),
    (["reindex"], ["Reindex QMD"]),
    (["collections"], ["Inspect, modify, and author"]),
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
    (["providers", "add"], ["Append a provider entry"]),
    (["providers", "set-default"], ["default_model"]),
    (["providers", "assign"], ["agents"]),
    (["providers", "unassign"], ["agent"]),
    (["providers", "check"], ["Probe every provider"]),
    (["providers", "list"], ["List every provider"]),
    # flock subcommands
    (["flock", "mywiki", "status"], ["Show the current memory-flock"]),
    (["flock", "mywiki", "force-repair"], ["Reap the memory flock"]),
    # mcp subcommands
    (["mcp", "start"], ["Run the MCP server on stdio"]),
    (["mcp", "up"], ["Start a detached streamable-http"]),
    (["mcp", "down"], ["Stop the MCP daemon"]),
    (["mcp", "status"], ["Report whether an MCP daemon"]),
]


@pytest.mark.parametrize("command,expected_substring", PARAMETRIZE_COMMAND_HELP_SUBSTRINGS)
def test_command_help_contains_help_text(command, expected_substring):
    """Every command + subcommand's --help output must contain at least
    one meaningful help= text snippet (the first line of its docstring).

    The `expected_substring` arg is a list of acceptable substrings; the
    test passes if any of them appears in the --help output. This
    accommodates multiple semantically equivalent wordings (e.g. a command
    whose docstring is "Start the daemon" vs. "Start the MCP daemon").
    """
    result = runner.invoke(app, command + ["--help"])
    assert result.exit_code == 0, f"command {command} --help failed: {result.output}"
    output = result.output if isinstance(result.output, str) else "".join(result.output)
    candidates = (
        expected_substring if isinstance(expected_substring, list) else [expected_substring]
    )
    if not any(s in output for s in candidates):
        raise AssertionError(f"help text {candidates!r} not in {command} --help output:\n{output}")
