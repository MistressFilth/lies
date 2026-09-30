"""Pin that a multi-word question reaches the prompt body parser intact.

The host's slash dispatcher pre-tokenizes on whitespace; tool-call
arguments do not. This test is the load-bearing proof that the
``get_prompt`` path carries the full string through to
``_parse_question_filters``.
"""

from __future__ import annotations

import pytest

from tests.unit.mcp._prompt_body import carries_verbatim, rendered_body


@pytest.mark.asyncio
async def test_render_carries_multiword_question() -> None:
    from lies.mcp.server import mcp

    prompt = await mcp.get_prompt("ask")
    assert prompt is not None
    result = await prompt.render({"question": "+c:test_alpha what does pydantic validate?"})
    [message] = result.messages
    body = rendered_body(message)
    # The question reaches both routed calls as a fenced verbatim
    # block, not a repr-escaped literal, so what the user typed is
    # what the agent passes.
    assert carries_verbatim(body, "what does pydantic validate?")
    assert body.count("what does pydantic validate?") == 2
    assert "tag_expr='c:test_alpha'" in body
    assert "exclude_tags=None" in body


@pytest.mark.asyncio
async def test_render_ground_excludes_lib_ask() -> None:
    from lies.mcp.server import mcp

    prompt = await mcp.get_prompt("ground")
    assert prompt is not None
    result = await prompt.render({"tail": "what is qmd"})
    [message] = result.messages
    body = rendered_body(message)
    assert "mcp__lies__lib_ask" not in body
    assert "digest-only" in body


@pytest.mark.asyncio
async def test_render_unknown_prompt_name_returns_none() -> None:
    from lies.mcp.server import mcp

    assert await mcp.get_prompt("no_such_prompt") is None
