"""Prompt implementations — module-level functions decorators wrap.

Each ``<name>_prompt`` function in this module returns a list of
``Message`` for the prompt it implements. ``register_all`` decorates
each one with ``@mcp.prompt(name=...)`` and binds it to the live
``mcp``. Tests drive the impl functions directly via
``prompts_impl.<name>_prompt(...)`` without spinning up an MCP
instance.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastmcp.prompts import Message  # noqa: F401  (re-exported for impl fns)

if TYPE_CHECKING:
    from fastmcp import FastMCP


def register_all(mcp: FastMCP) -> None:
    """Wire each impl function as ``@mcp.prompt`` on ``mcp``.

    Real prompt bodies land in tasks 3-9. This stub returns empty;
    populate as each task adds a prompt.
    """
    del mcp  # no-op until task 3


# Concrete impl functions added by tasks 3-9 below.
