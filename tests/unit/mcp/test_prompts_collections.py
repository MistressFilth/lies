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


def test_collections_refuses_a_flag_whose_value_would_become_the_name() -> None:
    """``show --tag cli``: ``show`` declares no ``--tag``, so the flag is
    unknown and ``cli`` falls through to the positional the body would
    have handed to ``collections_read(name=…)``.

    The note has to say the value was re-read as the name. "Ignored"
    alone reads to the agent as "I dropped your flag", and it then
    queries a collection the user never named.
    """
    [msg] = collections_prompt("show --tag cli")
    body = rendered_body(msg)
    assert "Cannot run" in body
    assert "'cli' after --tag" in body
    assert "Bash(" not in body
    assert "collections_read" not in body


def test_collections_refuses_a_verb_with_no_collection_name() -> None:
    """The placeholder used to reach the command line.

    ``Bash(lies library where '<slug>')`` exits 2 on the angle brackets
    and reads to the agent as a real argument. Every verb that needs a
    slug asks instead.
    """
    for tail, verb in (
        ("where", "where"),
        ("show", "show"),
        ("modify", "modify"),
        ("tag", "tag"),
        ("delete", "delete"),
        ("new", "new"),
    ):
        [msg] = collections_prompt(tail)
        body = rendered_body(msg)
        assert "Cannot run" in body, tail
        assert "No command was run" in body, tail
        assert "Bash(" not in body, tail
        assert f"'{verb}'" in body, tail


def test_collections_still_reports_a_surplus_positional() -> None:
    """A *declared* boolean followed by a bare word is a genuine surplus
    positional, not a re-purposed flag value — it is reported, and the
    command still renders."""
    [msg] = collections_prompt("delete mylib --force extra")
    body = rendered_body(msg)
    assert "Run Bash(lies library delete mylib --force)" in body
    assert "Not consumed by 'delete': extra" in body


def test_collections_tag_renders_the_flag_spelling() -> None:
    """``tag mylib --tag docs`` rendered ``lies library modify mylib``
    and dropped the tag: the branch read tags from the positionals
    only. Both spellings now reach the command."""
    [msg] = collections_prompt("tag mylib --tag docs")
    assert "Run Bash(lies library modify mylib --tag docs)" in rendered_body(msg)

    [msg] = collections_prompt("tag mylib docs")
    assert "Run Bash(lies library modify mylib --tag docs)" in rendered_body(msg)
