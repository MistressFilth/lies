"""Assert the FastMCP server registers exactly the documented surface."""

from __future__ import annotations

import pytest
from fastmcp import Client

from lies.mcp.server import mcp


@pytest.fixture
async def client() -> Client:
    async with Client(mcp) as c:
        yield c


async def test_server_name_is_lies() -> None:
    assert mcp.name == "lies"


async def test_tools_registered(client: Client) -> None:
    tools = await client.list_tools()
    names = {t.name for t in tools}
    assert names == {
        "init_wiki",
        "ingest_source",
        "query",
        "lint",
        "wiki_search",
        "wiki_read",
        "wiki_changes",
    }


async def test_resources_registered(client: Client) -> None:
    resources = await client.list_resources()
    uris = {str(r.uri) for r in resources}
    assert uris == {
        "wiki://status",
        "wiki://index",
        "wiki://log",
        "wiki://lint-report",
        "wiki://memory-changes",
        "wiki://catalog",
    }


async def test_resource_templates_registered(client: Client) -> None:
    templates = await client.list_resource_templates()
    # The MCP protocol layer exposes ``uriTemplate`` (camelCase) on
    # ``mcp.types.ResourceTemplate``; the high-level fastmcp object uses
    # ``uri_template`` but ``Client.list_resource_templates`` returns the
    # protocol-layer objects.
    patterns = {t.uriTemplate for t in templates}
    assert "wiki://page/{path}" in patterns
    assert "wiki://catalog/{slug}" in patterns


async def test_prompts_registered(client: Client) -> None:
    prompts = await client.list_prompts()
    names = {p.name for p in prompts}
    assert "ask_wiki" in names
