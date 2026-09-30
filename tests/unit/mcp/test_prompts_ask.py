"""Pin the ask prompt body routes through search, read, and lib_ask."""

from __future__ import annotations

from tests.unit.mcp._prompt_body import rendered_body


def test_ask_prompt_returns_single_message() -> None:
    from lies.mcp.prompts_impl import ask_prompt

    msgs = ask_prompt("how do I wire hooks")
    assert isinstance(msgs, list)
    assert len(msgs) == 1


def test_ask_prompt_body_names_routed_tools() -> None:
    from lies.mcp.prompts_impl import ask_prompt

    [msg] = ask_prompt("+c:test_alpha -t:draft anything")
    body = rendered_body(msg)
    assert "mcp__lies__search" in body
    assert "mcp__lies__read" in body
    assert "mcp__lies__lib_ask" in body
    # The tag atoms end up as repr'd string/list literals inside the
    # body — single quotes inside are repr-escaped because the body
    # itself is rendered via str(Message) which round-trips through
    # repr on TextContent. Match the escaped form.
    assert "tag_expr='c:test_alpha'" in body
    assert "exclude_tags=['t:draft']" in body


def test_ask_prompt_skips_cite_render_when_tag_is_none() -> None:
    """Empty tag_expr produces the same body — the LLM decides at render time."""
    from lies.mcp.prompts_impl import ask_prompt

    [msg_a] = ask_prompt("how does pydantic validate nested models")
    body = rendered_body(msg_a)
    # Lib_ask is still routed; tag_expr=None is templated as None.
    assert "mcp__lies__lib_ask" in body
    assert "tag_expr=None" in body
    assert "exclude_tags=None" in body


def test_ask_prompt_parses_filters_out_of_question() -> None:
    """``+tag`` and ``-tag`` markers are stripped from the question text
    and routed into ``tag_expr`` / ``exclude_tags`` on the dispatched
    tool calls."""
    from lies.mcp.prompts_impl import ask_prompt

    [msg] = ask_prompt("+c:alpha -t:draft my question")
    body = rendered_body(msg)
    assert "tag_expr='c:alpha'" in body
    assert "exclude_tags=['t:draft']" in body
    # Routed lib_ask carries the parsed filters (not raw question).
    assert "lib_ask('my question'" in body
    # Routed search carries the parsed filters too.
    assert "search('my question'" in body
    # The +tag/-tag tokens themselves must not leak into the body.
    assert "+c:alpha" not in body
    assert "-t:draft" not in body


def test_ask_prompt_no_filter_tokens_passes_question_through_untouched() -> None:
    """When no ``+tag`` / ``-tag`` markers are present, the question
    passes through verbatim and both routed args template as ``None``."""
    from lies.mcp.prompts_impl import ask_prompt

    [msg] = ask_prompt("plain question text")
    body = rendered_body(msg)
    assert "search('plain question text'" in body
    assert "lib_ask('plain question text'" in body
    assert "tag_expr=None" in body
    assert "exclude_tags=None" in body


def test_ask_prompt_multi_plus_tokens_combine_with_or() -> None:
    """Multiple ``+tag`` tokens are OR-joined with ``|`` per the
    canonical include-expression form."""
    from lies.mcp.prompts_impl import ask_prompt

    [msg] = ask_prompt("+c:alpha +c:beta my question")
    body = rendered_body(msg)
    assert "tag_expr='c:alpha|c:beta'" in body
    assert "lib_ask('my question'" in body


def test_ask_prompt_tolerates_an_apostrophe_in_the_question() -> None:
    """The prompt body's motivating question, verbatim.

    A ``shlex``-based splitter raised ``ValueError: No closing
    quotation`` here, which FastMCP surfaces as a ``PromptError`` --
    the prompt failed for the exact English a user is most likely to
    type. Question text is prose, not shell.
    """
    from lies.mcp.prompts_impl import ask_prompt

    [msg] = ask_prompt("what are Claude Code's plugin differences?")
    body = rendered_body(msg)
    assert 'search("what are Claude Code\'s plugin differences?"' in body
    assert 'lib_ask("what are Claude Code\'s plugin differences?"' in body


def test_ask_prompt_tolerates_an_unbalanced_quote() -> None:
    from lies.mcp.prompts_impl import ask_prompt

    [msg] = ask_prompt('what does "pydantic validate, exactly?')
    body = rendered_body(msg)
    assert "search('what does \"pydantic validate, exactly?'" in body


def test_ask_prompt_with_only_filter_tokens_asks_for_a_question() -> None:
    """A tail of nothing but ``+tag`` markers used to render a search for
    the empty string, which the qmd daemon treats as a real query."""
    from lies.mcp.prompts_impl import ask_prompt

    [msg] = ask_prompt("+c:opencode")
    body = rendered_body(msg)
    assert "No question given" in body
    assert "mcp__lies__search" not in body
