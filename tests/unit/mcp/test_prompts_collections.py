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
    assert "whitespace-separated" in body
    assert "quotes are literal here" in body
    # The command the agent would run is still rendered.
    assert "lies library modify claude_code" in body


def test_a_quoted_flag_value_is_named_not_silently_stored() -> None:
    """``--tag 'cli'`` stores the tag ``'cli'``, quotes included.

    The note used to inspect positionals only, so the most common way
    to quote -- a flag's value -- was unreported, and ``shlex.quote``
    faithfully passed the quote characters *into* the value.
    """
    [msg] = collections_prompt("modify claude_code --tag 'cli'")
    body = rendered_body(msg)
    assert "quotes are literal here" in body
    assert "--tag" in body


def test_a_quote_in_a_collection_name_is_json_not_shell_quoting() -> None:
    """An MCP tool argument is not a shell word.

    ``shlex.quote("a'b")`` renders the six-character string
    ``'"'"'b'``, so an agent copying the body looked up a collection
    literally named that.
    """
    [msg] = collections_prompt("show a'b")
    body = rendered_body(msg)
    assert 'name="a\'b"' in body, body


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


def test_new_says_when_a_source_positional_was_dropped_in_favour_of_a_flag() -> None:
    """The generic leftover note names the wrong cause.

    ``new mylib /tmp/src --prompt w.md`` renders the command *without*
    ``/tmp/src``, and the leftover note says "1 positional(s) the verb
    does not take" — but ``new`` does take a second positional; it is
    the source, and a ``--source``/``--prompt`` flag won. A user told
    the verb does not accept their source would conclude the flag
    invented a rule.
    """
    [msg] = collections_prompt("new mylib /tmp/src --prompt w.md")
    body = rendered_body(msg)
    assert "was the source you gave and the flag form won" in body, body
    assert "--prompt w.md" in body, body
    # The flag-wins case keeps the leftover note too; both statements
    # are true and the second is not a contradiction of the first.
    assert "Not consumed by 'new'" in body, body

    # A positional used as the source is *not* reported as dropped.
    [msg] = collections_prompt("new mylib /tmp/src")
    body = rendered_body(msg)
    assert "--source /tmp/src" in body, body
    assert "the flag form won" not in body, body
