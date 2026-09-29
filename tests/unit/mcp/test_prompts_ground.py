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
        "register a PostToolUse hook",
        tag_expr="c:test_alpha",
        exclude_tags=["t:draft"],
        top_k=5,
    )
    body = rendered_body(msg)
    assert "mcp__lies__search" in body
    assert "mcp__lies__read" in body
    assert "c:test_alpha" in body
    assert "top_k=5" in body


def test_ground_prompt_renders_cite_marker_form() -> None:
    from lies.mcp.prompts_impl import ground_prompt

    [msg] = ground_prompt("anything")
    body = rendered_body(msg)
    assert "[[collection/slug]]" in body
    assert "200" in body  # the ≤200-char snippet truncation
