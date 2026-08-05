from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path
from textwrap import dedent

import pytest

from lies.mcp.server import mcp
from tests.conftest import make_wiki


def asyncio_run(coro: object) -> object:
    return asyncio.run(coro)  # type: ignore[arg-type]


@pytest.fixture
def wiki(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Seed a wiki at the XDG data home so ``Wiki.require`` finds it.

    ``Wiki.require(name)`` looks under ``$XDG_DATA_HOME/lies/<name>``; the
    ``_isolated_xdg`` autouse fixture points that env var at
    ``tmp_path/xdg/data``, so we mirror the wiki's ``raw/``, ``wiki/``,
    and ``.git/`` into that location and set ``LIES_WIKI_NAME`` so the
    MCP server's ``resolve_wiki`` picks the same name.
    """
    from lies import xdg

    name = "mcp-memory"
    source_root = tmp_path / "wiki"
    for sub in ("wiki", "raw"):
        (source_root / sub).mkdir(parents=True)
    (source_root / "wiki" / "concepts").mkdir(parents=True)
    (source_root / "wiki" / "concepts" / "x.md").write_text(
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
    (source_root / "wiki" / "index.md").write_text("- [X](concepts/x.md)\n", encoding="utf-8")

    # Mirror the source wiki at the XDG data home so ``Wiki.require`` resolves it.
    target = xdg.data_home() / "lies" / name
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source_root, target)
    subprocess.run(["git", "init", "--initial-branch=main", str(target)], check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=target, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=target, check=True)
    subprocess.run(["git", "add", "."], cwd=target, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=target, check=True)

    # Touch the Wiki dataclass so callers using ``make_wiki`` agree with
    # what the MCP server resolves at runtime.
    make_wiki(name=name, data_root=target)
    monkeypatch.setenv("LIES_WIKI_NAME", name)
    return target


def test_wiki_search_registered() -> None:
    tools = {tool.name for tool in asyncio_run(mcp.list_tools())}
    assert "wiki_search" in tools
    assert "wiki_read" in tools


def test_wiki_search_returns_evidence(wiki: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "lies.qmd.cli.qmd_query",
        lambda *a, **kw: [{"path": "concepts/x.md", "score": 1.0}],
    )
    from fastmcp import Client
    from fastmcp.client.transports import FastMCPTransport

    transport = FastMCPTransport(mcp)
    client = Client(transport=transport)

    async def call() -> object:
        async with client:
            return await client.call_tool(
                "wiki_search", {"question": "X", "limit": 3, "name": "mcp-memory"}
            )

    result = asyncio.run(call())
    payload = result.data
    assert "pages" in payload
    assert payload["pages"]


def test_wiki_read_returns_page(wiki: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "lies.qmd.cli.qmd_query",
        lambda *a, **kw: [{"path": "concepts/x.md", "score": 1.0}],
    )
    from fastmcp import Client
    from fastmcp.client.transports import FastMCPTransport

    client = Client(FastMCPTransport(mcp))

    async def call() -> object:
        async with client:
            return await client.call_tool(
                "wiki_search", {"question": "X", "limit": 1, "name": "mcp-memory"}
            )

    result = asyncio.run(call())
    page_id = result.data["pages"][0]["page_id"]

    async def read() -> object:
        async with client:
            return await client.call_tool(
                "wiki_read", {"page_ids": [page_id], "name": "mcp-memory"}
            )

    read_result = asyncio.run(read())
    assert "The thing." in read_result.data[page_id]
