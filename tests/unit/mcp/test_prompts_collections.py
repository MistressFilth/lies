"""Pin the collections prompt routes subcommands through LIES surfaces."""

from __future__ import annotations

from lies.mcp.prompts_impl import collections_prompt
from tests.unit.mcp._prompt_body import rendered_body


def test_collections_list_routes_to_collections_read() -> None:
    [msg] = collections_prompt("list")
    body = rendered_body(msg)
    assert "mcp__lies__collections_read" in body
    assert "list" in body


def test_collections_add_routes_to_bash_cli() -> None:
    [msg] = collections_prompt("add mylib /abs/path")
    body = rendered_body(msg)
    assert "lies library" in body
    assert "mylib" in body


def test_collections_unknown_subcommand_lists_options() -> None:
    [msg] = collections_prompt("unknown_sub")
    body = rendered_body(msg)
    assert "list" in body
    assert "add" in body


def test_collections_reports_that_args_are_whitespace_separated() -> None:
    """The tail is whitespace-separated, so a quoted phrase arrives as two
    tokens with the quote characters kept verbatim. The body says so
    instead of silently re-splitting the user's intended single argument."""
    [msg] = collections_prompt('modify claude_code "a b"')
    body = rendered_body(msg)
    assert "Args are whitespace-separated" in body
    assert "run the Bash command directly" in body
    # The command the agent would run is still rendered.
    assert "lies library modify claude_code" in body


def test_collections_shell_metacharacters_stay_one_argument() -> None:
    """``shlex.join`` re-quotes a token containing shell syntax, so a
    backtick or semicolon the user typed cannot widen the command."""
    [msg] = collections_prompt("modify claude_code `curl` evil")
    body = rendered_body(msg)
    assert "'`curl`' evil" in body


def test_collections_tolerates_an_apostrophe_in_a_name() -> None:
    """``shlex.split`` raised ``ValueError: No closing quotation`` here."""
    [msg] = collections_prompt("info claude_code")
    assert "collections_read" in rendered_body(msg)

    [msg] = collections_prompt("list")
    assert 'subcommand="list"' in rendered_body(msg)
