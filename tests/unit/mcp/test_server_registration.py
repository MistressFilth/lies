"""MCP server registers the v0.40 tool surface (and no others).

The v0.40 rewrite retires seven prompts and eight tools, replacing
them with the new ``collections_read`` + ``search`` + ``read`` +
``ask`` surface. Drift on this contract (a forgotten registration
or a leaked legacy tool) makes every downstream caller version-
incompatible, so we pin the wire-shape contract here.

Each test introspects the live ``mcp`` object via FastMCP's
internal ``_local_provider._components`` registry — same data
the MCP wire sees, no extra Client boot. ``toolsets`` exists on
FastMCP main but not on FastMCP 4.x (the version pinned in this
repo), so the helper falls back to the ``_components`` dict.
"""

from __future__ import annotations

import pytest
from fastmcp import FastMCP

# Module scope, not inside each test body: importing lies.mcp.server builds the
# PromptsAsTools transform, and the first test to trigger that import pays
# the full cost inside its call phase.
from lies.mcp.server import mcp


def test_server_registers_collections_read() -> None:
    assert "collections_read" in _registered_tool_names(mcp)


def test_server_registers_search() -> None:
    assert "search" in _registered_tool_names(mcp)


def test_server_registers_read() -> None:
    assert "read" in _registered_tool_names(mcp)


def test_server_registers_lib_ask() -> None:
    registered = _registered_tool_names(mcp)
    assert "lib_ask" in registered, f"missing tool: 'lib_ask' in {registered}"
    assert "ask" not in registered, f"deprecated 'ask' tool still registered: {registered}"


def test_server_registers_lint_and_reindex() -> None:
    registered = _registered_tool_names(mcp)
    assert {"lint", "reindex"} <= registered, (
        f"missing tools from v0.40 surface: {{'lint', 'reindex'}} - {registered}"
    )


@pytest.mark.slow
def test_server_drops_old_tools() -> None:
    forbidden = {
        "wiki_search",
        "wiki_read",
        "wiki_catalog",
        "synthesize",
        "ground",
        "ask_question",
        "ask_ground_question",
        "init_wiki",
    }
    registered = _registered_tool_names(mcp)
    leaked = registered & forbidden
    assert not leaked, f"old tools still registered: {sorted(leaked)}"


def test_server_registers_seven_prompts() -> None:
    """Slash-command prompt surface — exactly 7 expected names.

    Pins the wire-shape contract across all prompt registrations.
    Drift on this contract (any missing/extra prompt name in the live
    server `mcp` list) makes hosts render the wrong slash slot, so
    the test xfails until task 9 (the final prompt registration)
    flips it green.
    """
    registered = _registered_prompt_names(mcp)
    expected = {"ask", "collections", "ingest", "lint", "reindex", "sync", "ground"}
    missing = expected - registered
    extra = registered - expected
    assert not missing and not extra, (
        f"prompt set drift — missing: {sorted(missing)}, extra: {sorted(extra)}"
    )


# ---------------------------------------------------------------------------
# FastMCP 4.x introspection helpers
# ---------------------------------------------------------------------------


def _registered_tool_names(mcp: FastMCP) -> set[str]:
    """Recover the set of registered tool names on this ``FastMCP`` instance.

    FastMCP 4.x stores components on ``_local_provider._components``,
    keyed ``tool:<name>@``. Falls back to ``mcp.toolsets`` when running
    against FastMCP main where the API surface is richer.
    """
    names: set[str] = set()
    for ts in getattr(mcp, "toolsets", None) or []:
        names.update(getattr(ts, "tools", {}).keys())
    provider = getattr(mcp, "_local_provider", None)
    if provider is not None:
        for key in getattr(provider, "_components", None) or {}:
            if key.startswith("tool:"):
                n = key.split(":", 1)[1].rstrip("@")
                if n:
                    names.add(n)
    return names


def _registered_prompt_names(mcp: FastMCP) -> set[str]:
    """Recover the set of registered prompt names on this ``FastMCP`` instance.

    Mirrors :func:`_registered_tool_names` for the ``prompt:<name>@``
    key prefix in ``_local_provider._components``. Robust to FastMCP's
    middle-of-release refactors by walking whichever attribute carries
    the component registry.
    """
    names: set[str] = set()
    provider = getattr(mcp, "_local_provider", None)
    if provider is not None:
        for key in getattr(provider, "_components", None) or {}:
            if key.startswith("prompt:"):
                n = key.split(":", 1)[1].rstrip("@")
                if n:
                    names.add(n)
    pm = getattr(mcp, "_prompt_manager", None)
    if pm is not None:
        listed = getattr(pm, "list_prompts", None)
        if listed is not None:
            names.update(p.name for p in listed())
    return names
