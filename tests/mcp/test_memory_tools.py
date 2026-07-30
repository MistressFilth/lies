from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from textwrap import dedent

import pytest

from lies.mcp.server import mcp


def asyncio_run(coro: object) -> object:
    return asyncio.run(coro)  # type: ignore[arg-type]


@pytest.fixture
def wiki(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "wiki"
    for sub in ("wiki", ".lies", "raw"):
        (root / sub).mkdir(parents=True)
    (root / "wiki" / "concepts").mkdir(parents=True)
    (root / "wiki" / "concepts" / "x.md").write_text(
        dedent(
            """\
            ---
            title: X
            type: concept
            ---
            # X

            The thing.
            """
        ),
        encoding="utf-8",
    )
    (root / "wiki" / "index.md").write_text(
        "- [X](concepts/x.md)\n", encoding="utf-8"
    )
    subprocess.run(["git", "init", "--initial-branch=main", str(root)], check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True)
    monkeypatch.setenv("LIES_WIKI_ROOT", str(root))
    return root


def test_wiki_search_registered() -> None:
    tools = {tool.name for tool in asyncio_run(mcp.list_tools())}
    assert "wiki_search" in tools
    assert "wiki_read" in tools


def test_wiki_search_returns_evidence(wiki: Path) -> None:
    from fastmcp import Client
    from fastmcp.client.transports import FastMCPTransport

    transport = FastMCPTransport(mcp)
    client = Client(transport=transport)

    async def call() -> object:
        async with client:
            return await client.call_tool("wiki_search", {"question": "X", "limit": 3})

    result = asyncio.run(call())
    payload = result.data
    assert "pages" in payload
    assert payload["pages"]


def test_wiki_read_returns_page(wiki: Path) -> None:
    from fastmcp import Client
    from fastmcp.client.transports import FastMCPTransport

    client = Client(transport=FastMCPTransport(mcp))

    async def call() -> object:
        async with client:
            return await client.call_tool("wiki_search", {"question": "X", "limit": 1})

    result = asyncio.run(call())
    page_id = result.data["pages"][0]["page_id"]

    async def read() -> object:
        async with client:
            return await client.call_tool("wiki_read", {"page_ids": [page_id]})

    read_result = asyncio.run(read())
    assert "The thing." in read_result.data[page_id]
