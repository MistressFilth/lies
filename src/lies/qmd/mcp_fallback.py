"""In-process FastMCP fallback for the qmd HTTP daemon.

When the qmd daemon is unreachable, this server provides a degraded
search surface for the agent's `wiki_search` and `wiki_read` tools.
It re-uses the same :class:`WikiMemoryService` the host path uses,
so a wiki that already degrades honestly through the host falls back
to the same data here.

The tool surface is intentionally narrow — only the two tools the
agent actually uses for retrieval. ``qmd_query`` / ``qmd_get`` /
``qmd_status`` / ``qmd_update`` are not re-implemented. This is the
honest "I have less capability than the daemon" surface.

Every result carries ``degraded: True`` plus the same
``fallback_reason`` the host path uses, so a model that reads the
result knows the daemon was not consulted.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from pydantic_ai.mcp import MCPToolset

    from lies.memory.service import WikiMemoryService
    from lies.wiki.wiki import Wiki


_FALLBACK_TOOL_NAMES = ("wiki_search", "wiki_read")


class _DegradedSearchResult(BaseModel):
    """Wire shape returned by the fallback's ``wiki_search`` tool."""

    query: str
    pages: list[dict[str, Any]]
    truncated: bool
    fallback_used: bool
    fallback_reason: str
    degraded: bool = Field(
        default=True,
        description="Always True. Indicates the search ran without the qmd daemon.",
    )


class QmdFallbackMcp:
    """In-process FastMCP server that fronts :class:`WikiMemoryService`."""

    def __init__(self, wiki: Wiki) -> None:
        self._wiki = wiki
        self._server: FastMCP | None = None
        self._toolset: MCPToolset[None] | None = None

    def _build_server(self) -> FastMCP:
        from fastmcp import FastMCP

        server = FastMCP("lies-qmd-fallback")
        service = self._make_service()

        @server.tool(
            name="wiki_search",
            description=(
                "Search the wiki for project knowledge relevant to the question. "
                "This is the degraded fallback — the qmd daemon is unreachable, so "
                "results come from a coarse index scan rather than a hybrid BM25+vector "
                "search. Coverage is narrower than the healthy path."
            ),
        )
        def wiki_search(question: str, limit: int = 5) -> _DegradedSearchResult:
            result = service.search(question, limit=limit)
            return _DegradedSearchResult(
                query=result.query,
                pages=[page.model_dump() for page in result.pages],
                truncated=result.truncated,
                fallback_used=result.fallback_used,
                fallback_reason=result.fallback_reason or "qmd_unavailable",
            )

        @server.tool(
            name="wiki_read",
            description="Read full wiki pages by ID. Identical contract to the healthy path.",
        )
        def wiki_read(page_ids: list[str]) -> dict[str, str]:
            return service.read(page_ids)

        return server

    def _make_service(self) -> WikiMemoryService:
        from lies.memory.service import WikiMemoryService

        return WikiMemoryService(self._wiki)

    # Direct call surface for tests — match the tool implementation
    # byte-for-byte so the tests do not need the async MCP client.

    def call_wiki_search(
        self,
        service: WikiMemoryService,
        *,
        question: str,
        limit: int = 5,
    ) -> _DegradedSearchResult:
        result = service.search(question, limit=limit)
        return _DegradedSearchResult(
            query=result.query,
            pages=[page.model_dump() for page in result.pages],
            truncated=result.truncated,
            fallback_used=result.fallback_used,
            fallback_reason=result.fallback_reason or "qmd_unavailable",
        )

    def call_wiki_read(
        self,
        service: WikiMemoryService,
        *,
        page_ids: list[str],
    ) -> dict[str, str]:
        return service.read(page_ids)

    def tools_known_to_model(self) -> list[str]:
        return list(_FALLBACK_TOOL_NAMES)

    def as_toolset(self) -> MCPToolset[None]:
        from fastmcp import Client
        from pydantic_ai.mcp import MCPToolset

        if self._server is None:
            self._server = self._build_server()
        if self._toolset is None:
            client = Client(self._server)
            self._toolset = MCPToolset(client, id="lies.qmd.fallback")
        return self._toolset
