"""The PromptsAsTools transform registration + name-collision guard."""

from __future__ import annotations

import pytest


def _effective_tool_names() -> set[str]:
    import asyncio

    from lies.mcp.server import mcp

    return {t.name for t in asyncio.run(mcp.list_tools())}


def test_transform_registers_prompt_tools() -> None:
    names = _effective_tool_names()
    assert {"list_prompts", "get_prompt"} <= names, (
        f"PromptsAsTools tools missing from {sorted(names)}"
    )


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


def test_transform_preserves_prompt_surface() -> None:
    """The transform is additive on the prompt side too — all seven stay."""
    import asyncio

    from lies.mcp.server import mcp

    prompts = {p.name for p in asyncio.run(mcp.list_prompts())}
    expected = {"ask", "ground", "collections", "ingest", "lint", "reindex", "sync"}
    missing = expected - prompts
    assert not missing, f"transform shadowed prompts: {sorted(missing)}"


@pytest.mark.asyncio
async def test_get_prompt_tool_is_callable() -> None:
    """End-to-end through the generated tool, not just the Prompt object."""
    from fastmcp import Client

    from lies.mcp.server import mcp

    async with Client(mcp) as client:
        result = await client.call_tool(
            "get_prompt",
            {
                "name": "ask",
                "arguments": {"tail": "+c:test_alpha what does pydantic validate?"},
            },
        )

    text = str(result.data)
    assert "what does pydantic validate?" in text, "the question text survived verbatim"
    assert "tag_expr='c:test_alpha'" in text, "the +tag filter was separated out"
    assert "exclude_tags=None" in text
    assert text.count("what does pydantic validate?") == 2, "both search and lib_ask got it"
