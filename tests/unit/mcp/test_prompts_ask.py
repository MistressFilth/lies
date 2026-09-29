"""Pin the ask prompt body routes through search, read, and lib_ask."""

from __future__ import annotations


def test_ask_prompt_returns_single_message() -> None:
    from lies.mcp.prompts_impl import ask_prompt

    msgs = ask_prompt("how do I wire hooks")
    assert isinstance(msgs, list)
    assert len(msgs) == 1


def test_ask_prompt_body_names_routed_tools() -> None:
    from lies.mcp.prompts_impl import ask_prompt

    [msg] = ask_prompt("anything", tag_expr="c:opencode", exclude_tags=["t:draft"])
    body = msg.text if hasattr(msg, "text") else str(msg)
    assert "mcp__lies__search" in body
    assert "mcp__lies__read" in body
    assert "mcp__lies__lib_ask" in body
    assert "c:opencode" in body


def test_ask_prompt_skips_cite_render_when_tag_is_none() -> None:
    """Empty tag_expr produces the same body — the LLM decides at render time."""
    from lies.mcp.prompts_impl import ask_prompt

    [msg_a] = ask_prompt("how does pydantic validate nested models")
    body = msg_a.text if hasattr(msg_a, "text") else str(msg_a)
    # Lib_ask is still routed; tag_expr=None is templated as None.
    assert "mcp__lies__lib_ask" in body
    assert "tag_expr=None" in body
