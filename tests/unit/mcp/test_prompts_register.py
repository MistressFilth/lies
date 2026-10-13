"""Pin the prompts module registers all 7 slash-command prompts."""

from __future__ import annotations

import pytest
from fastmcp import FastMCP


def test_register_prompts_callable() -> None:
    from lies.mcp.prompts import register_prompts

    assert callable(register_prompts)


@pytest.mark.slow
def test_register_prompts_idempotent_after_task_9() -> None:
    """Asserts 7 names bind and the registration call is idempotent."""
    from lies.mcp.prompts import register_prompts

    from tests.unit.mcp.test_server_registration import _registered_prompt_names

    mcp = FastMCP("test")
    register_prompts(mcp)
    names_first = _registered_prompt_names(mcp)
    register_prompts(mcp)
    names_second = _registered_prompt_names(mcp)
    assert names_first == names_second
    expected = {"ask", "collections", "ingest", "lint", "reindex", "sync", "ground"}
    assert expected <= names_first, f"missing prompts: {sorted(expected - names_first)}"
