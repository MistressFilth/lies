"""Pin the prompts module registers all 7 slash-command prompts."""

from __future__ import annotations

import pytest
from fastmcp import FastMCP


def test_register_prompts_callable() -> None:
    from lies.mcp.prompts import register_prompts

    assert callable(register_prompts)


@pytest.mark.xfail(reason="populated by task 9", strict=False)
def test_register_prompts_idempotent_after_task_9() -> None:
    """Asserts 7 names are bound. Enabled by task 9's final
    registration. Until then, skip with a module-level xfail."""
    from lies.mcp.prompts import register_prompts

    mcp = FastMCP("test")
    register_prompts(mcp)
    names_first = _names(mcp)
    register_prompts(mcp)
    names_second = _names(mcp)
    assert names_first == names_second
    expected = {"ask", "collections", "ingest", "lint", "reindex", "sync", "ground"}
    assert expected <= names_first, f"missing prompts: {sorted(expected - names_first)}"


def _names(mcp: FastMCP) -> set[str]:
    names: set[str] = set()
    provider = getattr(mcp, "_local_provider", None)
    if provider is not None:
        for key in getattr(provider, "_components", None) or {}:
            if key.startswith("prompt:"):
                n = key.split(":", 1)[1].rstrip("@")
                if n:
                    names.add(n)
    pm = getattr(mcp, "_prompt_manager", None)
    if pm is not None and getattr(pm, "list_prompts", None) is not None:
        names.update(p.name for p in pm.list_prompts())
    return names
