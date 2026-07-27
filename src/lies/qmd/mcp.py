"""qmd MCP client.

The qmd MCP server exposes `query`, `get`, `multi_get`, `status` tools
that the LIES orchestrator uses for hybrid search.

Usage:
    from pydantic_ai import Agent
    from lies.qmd.mcp import QmdMcpClient

    qmd = QmdMcpClient(transport="stdio")
    agent = Agent("anthropic:claude-opus-4-7", capabilities=[qmd.as_capability()])

Note: the stdio transport needs pydantic-ai's `mcp` extra installed at
runtime — `pip install "pydantic-ai-slim[mcp]"`. Without it the capability
constructs fine but tool calls will fail with an ImportError explaining
the missing dependency.

For stdio the qmd binary is launched as `qmd mcp` through a FastMCP
`StdioTransport`, which is the invocation required for qmd's MCP server.

See https://github.com/tobi/qmd#mcp for the qmd MCP surface.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class QmdMcpClient:
    """Connection config for the qmd MCP server.

    Attributes:
        transport: Either "stdio" (default; spawns `qmd` as an MCP server)
            or "http" (connects to a running `qmd mcp --http` server).
        url: Required when transport is "http". Defaults to
            "http://localhost:8181".
    """

    transport: str = "stdio"
    url: str = "http://localhost:8181"

    def as_capability(self) -> Any:
        """Return a pydantic-ai capability that exposes qmd's MCP tools.

        The returned object can be passed to `Agent(capabilities=[...])`.
        """
        from pydantic_ai.capabilities import MCP

        if self.transport == "stdio":
            # Local stdio server. The capability needs an `MCPToolset`,
            # which lives behind the `mcp` extra. Defer construction to a
            # factory so the capability can be built without the extra
            # installed; the factory is only invoked when the agent actually
            # wires up its toolsets at runtime.
            def _build_qmd_stdio_toolset() -> Any:
                from fastmcp import Client  # type: ignore[import-not-found]
                from fastmcp.client.transports import (  # type: ignore[import-not-found]
                    StdioTransport,
                )
                from pydantic_ai.mcp import MCPToolset

                client = Client(StdioTransport(command="qmd", args=["mcp"]))
                return MCPToolset(client)

            return MCP(local=_build_qmd_stdio_toolset)
        if self.transport == "http":
            return MCP(url=self.url, native=True, local=False)
        raise ValueError(f"Unknown transport: {self.transport}")
