"""MCP server package.

Re-exports the FastMCP server instance for tool implementations.
"""

from __future__ import annotations

__all__ = ["mcp"]


# Imported lazily so importing the package without FastMCP installed
# still works for the resolution helpers.
def __getattr__(name: str) -> object:
    if name == "mcp":
        from lies.mcp.server import mcp

        return mcp
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
