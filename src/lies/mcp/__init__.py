"""MCP server package.

Re-exports the FastMCP server instance and the grounding archivist's
``ground`` helper for in-process callers. ``ground`` is shipped at
module scope on ``lies.mcp.grounding`` (Task 2); exposing it here
gives callers one import surface (``from lies.mcp import ground``)
without pulling FastMCP into the path.
"""

from __future__ import annotations

__all__ = ["ground", "mcp"]


# Imported lazily so importing the package without FastMCP installed
# still works for the resolution helpers.
def __getattr__(name: str) -> object:
    if name == "mcp":
        from lies.mcp.server import mcp

        return mcp
    if name == "ground":
        from lies.mcp.grounding import ground

        return ground
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
