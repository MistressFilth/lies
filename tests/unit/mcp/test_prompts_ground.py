"""Pin the ground prompt body folds search/read snippets into citations."""

from __future__ import annotations

from tests.unit.mcp._prompt_body import carries_verbatim, rendered_body


def test_ground_prompt_skips_lib_ask() -> None:
    from lies.mcp.prompts_impl import ground_prompt

    [msg] = ground_prompt("how do I configure hooks")
    body = rendered_body(msg)
    assert "mcp__lies__lib_ask" not in body, (
        "ground prompt must NOT route through lib_ask — that is the ask slash's job"
    )


def test_ground_prompt_routes_search_and_read() -> None:
    from lies.mcp.prompts_impl import ground_prompt

    [msg] = ground_prompt("+c:test_alpha -t:draft --top_k=5 register a PostToolUse hook")
    body = rendered_body(msg)
    assert "mcp__lies__search" in body
    assert "mcp__lies__read" in body
    assert "tag_expr='c:test_alpha'" in body
    assert "exclude_tags=['t:draft']" in body
    assert "top_k=5" in body


def test_ground_prompt_renders_cite_marker_form() -> None:
    from lies.mcp.prompts_impl import ground_prompt

    [msg] = ground_prompt("anything")
    body = rendered_body(msg)
    assert "[[collection/slug]]" in body
    assert "200" in body  # the ≤200-char snippet truncation


def test_ground_prompt_parses_excludes_correctly() -> None:
    """``-tag`` markers are routed into ``exclude_tags`` on the
    dispatched search call."""
    from lies.mcp.prompts_impl import ground_prompt

    [msg] = ground_prompt("-t:draft -t:wip any question text")
    body = rendered_body(msg)
    assert "exclude_tags=['t:draft', 't:wip']" in body
    assert "tag_expr=None" in body
    assert carries_verbatim(body, "any question text")
    # Filter tokens themselves must not leak into the routed args.
    assert "-t:draft" not in body
    assert "-t:wip" not in body


def test_ground_prompt_no_filter_tokens() -> None:
    """Without ``+tag`` / ``-tag`` markers, both filters are ``None``."""
    from lies.mcp.prompts_impl import ground_prompt

    [msg] = ground_prompt("plain question text")
    body = rendered_body(msg)
    assert carries_verbatim(body, "plain question text")
    assert "tag_expr=None" in body
    assert "exclude_tags=None" in body


def test_ground_prompt_tolerates_an_apostrophe() -> None:
    """Regression: ``shlex.split`` raised ``ValueError: No closing
    quotation`` on any apostrophe, failing 6 of the 7 prompts."""
    from lies.mcp.prompts_impl import ground_prompt

    [msg] = ground_prompt("what are Claude Code's plugin differences?")
    body = rendered_body(msg)
    assert "mcp__lies__search" in body
    assert "what are Claude Code's plugin differences?" in body


def test_ground_top_k_accepts_the_space_form() -> None:
    """Only ``--top_k=N`` used to bind. With the space form, ``N`` leaked
    into the query text and ``top_k`` silently fell back to the default."""
    from lies.mcp.prompts_impl import ground_prompt

    [msg] = ground_prompt("--top_k 7 the question")
    body = rendered_body(msg)
    assert carries_verbatim(body, "the question")
    assert "top_k=7" in body


def test_ground_top_k_clamps_out_of_range_values() -> None:
    from lies.mcp.prompts_impl import ground_prompt

    for tail, expected in (("--top_k 99 q", "top_k=10"), ("--top_k 0 q", "top_k=1")):
        [msg] = ground_prompt(tail)
        assert expected in rendered_body(msg)

    [msg] = ground_prompt("--top_k notanumber q")
    assert "top_k=3" in rendered_body(msg)


def test_ground_empty_tail_asks_for_a_question() -> None:
    """An empty tail used to render ``search('')``."""
    from lies.mcp.prompts_impl import ground_prompt

    [msg] = ground_prompt("")
    body = rendered_body(msg)
    assert "No question given" in body
    assert "mcp__lies__search" not in body


def test_ground_a_tail_of_only_filters_asks_for_a_question() -> None:
    """The guard ran before the filter pass, so it never fired.

    A filter token is itself a positional, so ``+c:opencode`` and a
    bare ``--top_k 5`` both satisfied the old emptiness check and
    rendered ``search('')`` -- a query the qmd daemon treats as real.
    ``ask`` had the check in the right place all along; ``ground`` did
    not, and the CHANGELOG claimed both were fixed.
    """
    from lies.mcp.prompts_impl import ground_prompt

    for tail in ("+c:opencode", "-t:draft", "--top_k 5", "--top_k=5"):
        [msg] = ground_prompt(tail)
        body = rendered_body(msg)
        assert "No question given" in body, f"{tail!r} should ask for a question:\n{body}"
        assert "mcp__lies__search" not in body, f"{tail!r} rendered a search:\n{body}"


def test_ground_filters_plus_a_question_still_render() -> None:
    """The post-extraction guard must not eat a real query."""
    from lies.mcp.prompts_impl import ground_prompt

    [msg] = ground_prompt("+c:opencode what is qmd")
    body = rendered_body(msg)
    assert carries_verbatim(body, "what is qmd")
    assert "tag_expr='c:opencode'" in body


def test_ground_names_an_unrecognized_flag() -> None:
    """``--cleaup`` used to be dropped silently; a typo in a destructive
    flag must not run the non-destructive path unremarked."""
    from lies.mcp.prompts_impl import ground_prompt

    [msg] = ground_prompt("--cleaup 5 the question")
    body = rendered_body(msg)
    assert "Unrecognized flag(s) ignored: --cleaup." in body


def test_ground_keeps_negative_numbers_in_the_query() -> None:
    from lies.mcp.prompts_impl import ground_prompt

    [msg] = ground_prompt("why is -1 broken here")
    body = rendered_body(msg)
    assert carries_verbatim(body, "why is -1 broken here")
    assert "exclude_tags=None" in body


def test_ground_names_a_malformed_top_k_instead_of_silently_defaulting() -> None:
    """Every other malformed tail in this module gets a named note; a
    swallowed ``--top_k`` read to the user as "you asked for 5, got 3"
    with no cause attached."""
    from lies.mcp.prompts_impl import ground_prompt

    [msg] = ground_prompt("--top_k abc what changed")
    body = rendered_body(msg)
    assert "top_k=3 entries" in body
    assert "--top_k='abc' is not an integer" in body

    [msg] = ground_prompt("--top_k 99 what changed")
    body = rendered_body(msg)
    assert "top_k=10 entries" in body
    assert "--top_k=99 is outside [1, 10]; clamped to 10." in body


def test_ground_stays_quiet_about_a_well_formed_top_k() -> None:
    from lies.mcp.prompts_impl import ground_prompt

    [msg] = ground_prompt("--top_k 5 what changed")
    body = rendered_body(msg)
    assert "top_k=5 entries" in body
    assert "top_k" not in body.rsplit("clamped to", 1)[-1].split("entries")[-1]
