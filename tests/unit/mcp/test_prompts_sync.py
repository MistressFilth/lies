"""Pin the sync prompt routes through Bash(lies sync ...)."""

from __future__ import annotations

from tests.unit.mcp._prompt_body import rendered_body


def test_sync_prompt_default_targets_all_collections() -> None:
    from lies.mcp.prompts_impl import sync_prompt

    [msg] = sync_prompt()
    body = rendered_body(msg)
    assert "lies sync" in body
    assert "Bash(lies sync --data-dir" in body


def test_sync_prompt_with_collections_passes_only_flag() -> None:
    from lies.mcp.prompts_impl import sync_prompt

    [msg] = sync_prompt(collections=["pydantic", "fastmcp"])
    body = rendered_body(msg)
    assert "--only" in body
    assert "pydantic" in body
    assert "fastmcp" in body


def test_sync_prompt_with_dry_run_flag_passes_through() -> None:
    from lies.mcp.prompts_impl import sync_prompt

    [msg] = sync_prompt(dry_run=True)
    body = rendered_body(msg)
    assert "--dry-run" in body


def test_sync_prompt_threads_scraper_timeout() -> None:
    from lies.mcp.prompts_impl import sync_prompt

    [msg] = sync_prompt(scraper_timeout=600)
    body = rendered_body(msg)
    assert "--scraper-timeout 600" in body
