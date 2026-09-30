"""Pin the PromptsAsTools transform registration + name-collision guard.

The transform generates two unprefixed tool names (``list_prompts`` and
``get_prompt``). Nothing else on the LIES server registers those names
today; this test is the cheap insurance against a future collision.

Tool names are read through ``mcp.list_tools()`` — the public
aggregation path. Transforms run at aggregation time, so the raw
``_local_provider._components`` registry does not carry the
transform-generated names and would report a false negative.
"""

from __future__ import annotations

import pytest


def _effective_tool_names() -> set[str]:
    import asyncio

    from lies.mcp.server import mcp

    return {t.name for t in asyncio.run(mcp.list_tools())}


@pytest.mark.slow
def test_transform_registers_prompt_tools() -> None:
    names = _effective_tool_names()
    assert {"list_prompts", "get_prompt"} <= names, (
        f"PromptsAsTools tools missing from {sorted(names)}"
    )


@pytest.mark.slow
def test_transform_does_not_shadow_existing_tools() -> None:
    """The transform is additive — the six direct tools must survive."""
    names = _effective_tool_names()
    direct = {
        "collections_read",
        "search",
        "read",
        "lib_ask",
        "lint",
        "reindex",
    }
    missing = direct - names
    assert not missing, f"transform shadowed direct tools: {sorted(missing)}"


@pytest.mark.slow
def test_transform_preserves_prompt_surface() -> None:
    """The transform is additive on the prompt side too — all seven stay."""
    import asyncio

    from lies.mcp.server import mcp

    prompts = {p.name for p in asyncio.run(mcp.list_prompts())}
    expected = {"ask", "ground", "collections", "ingest", "lint", "reindex", "sync"}
    missing = expected - prompts
    assert not missing, f"transform shadowed prompts: {sorted(missing)}"


@pytest.mark.asyncio
@pytest.mark.slow
async def test_get_prompt_tool_is_callable() -> None:
    """End-to-end through the generated tool, not just the Prompt object.

    This is the load-bearing proof for the whole design: a multi-word
    question carrying a ``+tag`` filter reaches the prompt body parser
    intact, with the filter separated from the query text.
    """
    from fastmcp import Client

    from lies.mcp.server import mcp

    async with Client(mcp) as client:
        result = await client.call_tool(
            "get_prompt",
            {
                "name": "ask",
                "arguments": {"question": "+c:test_alpha what does pydantic validate?"},
            },
        )

    text = str(result.data)
    assert (
        "mcp__lies__search('what does pydantic validate?', "
        "tag_expr='c:test_alpha', exclude_tags=None)" in text
    )
    assert (
        "mcp__lies__lib_ask('what does pydantic validate?', "
        "tag_expr='c:test_alpha', exclude_tags=None)" in text
    )
