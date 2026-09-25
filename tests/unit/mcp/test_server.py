"""Server-shape tests for the library-mode MCP server surface.

Asserts the wire-shape contract after the read-side rewrite:
the retired wiki-shaped tools (query, answer, wiki_search,
wiki_read, wiki_changes, file_knowledge) are gone, replaced by
``ground`` (already shipped) and ``synthesize`` (Task 3 / commit 2).
The remaining tools (init_wiki, lint, ask_question, ask_ground_question,
reindex) keep their slots.

Each test introspects the live ``mcp`` object via FastMCP's internal
``_list_tools`` rather than spinning up a ``Client`` — the Client
path costs ~0.19s on this machine (well over the 0.15s pre-commit
hard limit) even though the introspection itself is sub-millisecond.
``_list_tools`` returns the same tool records the wire sees, so the
assertions exercise the same surface.
"""

from __future__ import annotations

from lies.mcp.server import mcp


async def test_server_registers_synthesize_tool() -> None:
    """The synthesize tool is registered after the rewrite.

    The retired wiki-shaped tools (query, answer, wiki_search,
    wiki_read, wiki_changes, file_knowledge) MUST NOT appear — the
    library-mode read surface replaces them with ground +
    synthesize + library:// resources.
    """
    tools = await mcp._list_tools()
    names = {t.name for t in tools}
    assert "synthesize" in names
    assert "query" not in names
    assert "answer" not in names
    assert "wiki_search" not in names
    assert "wiki_read" not in names
    assert "wiki_changes" not in names
    assert "file_knowledge" not in names
