"""Pin the reindex prompt routes through mcp__lies__reindex with flags."""

from __future__ import annotations

from tests.unit.mcp._prompt_body import rendered_body


def test_reindex_prompt_default_routes_update_only() -> None:
    from lies.mcp.prompts_impl import reindex_prompt

    [msg] = reindex_prompt("")
    body = rendered_body(msg)
    assert "mcp__lies__reindex" in body
    assert "reconcile=False" in body
    assert "embed=False" in body


def test_reindex_prompt_with_destructive_flag_warns_elicit() -> None:
    from lies.mcp.prompts_impl import reindex_prompt

    [msg] = reindex_prompt("--all")
    body = rendered_body(msg)
    assert "elicit" in body.lower() or "confirm" in body.lower()
    assert "all_=True" in body


def test_reindex_prompt_with_embed_routes_embed_flag() -> None:
    from lies.mcp.prompts_impl import reindex_prompt

    [msg] = reindex_prompt("--embed --force")
    body = rendered_body(msg)
    assert "embed=True" in body
    assert "force=True" in body
