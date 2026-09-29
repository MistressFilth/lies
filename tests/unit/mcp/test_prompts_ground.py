"""Pin the ground prompt body folds search/read snippets into citations."""

from __future__ import annotations

from tests.unit.mcp._prompt_body import rendered_body


def test_ground_prompt_skips_lib_ask() -> None:
    from lies.mcp.prompts_impl import ground_prompt

    [msg] = ground_prompt("how do I configure hooks")
    body = rendered_body(msg)
    assert "mcp__lies__lib_ask" not in body, (
        "ground prompt must NOT route through lib_ask — that is the ask slash's job"
    )


def test_ground_prompt_routes_search_and_read() -> None:
    from lies.mcp.prompts_impl import ground_prompt

    [msg] = ground_prompt(
        "+c:test_alpha -t:draft register a PostToolUse hook",
        top_k=5,
    )
    body = rendered_body(msg)
    assert "mcp__lies__search" in body
    assert "mcp__lies__read" in body
    # Single quotes inside the templated args are repr-escaped because
    # the body round-trips through repr on TextContent.
    assert "tag_expr=\\'c:test_alpha\\'" in body
    assert "exclude_tags=[\\'t:draft\\']" in body
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
    assert "exclude_tags=[\\'t:draft\\', \\'t:wip\\']" in body
    assert "tag_expr=None" in body
    assert "search(\\'any question text\\'" in body
    # Filter tokens themselves must not leak into the routed args.
    assert "-t:draft" not in body
    assert "-t:wip" not in body


def test_ground_prompt_no_filter_tokens() -> None:
    """Without ``+tag`` / ``-tag`` markers, both filters are ``None``."""
    from lies.mcp.prompts_impl import ground_prompt

    [msg] = ground_prompt("plain question text")
    body = rendered_body(msg)
    assert "search(\\'plain question text\\'" in body
    assert "tag_expr=None" in body
    assert "exclude_tags=None" in body
