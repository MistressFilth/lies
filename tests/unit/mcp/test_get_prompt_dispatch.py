"""Pin dispatch paths the PromptsAsTools transform does NOT cover.

``test_prompts_as_tools::test_get_prompt_tool_is_callable`` covers the
generated-tool end-to-end; this file covers the Prompt-object and
``mcp.get_prompt`` paths directly.
"""

from __future__ import annotations

import pytest

from tests.unit.mcp._prompt_body import rendered_body


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
