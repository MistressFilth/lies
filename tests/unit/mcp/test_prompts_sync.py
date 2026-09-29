"""Pin the sync prompt routes through Bash(lies sync ...)."""

from __future__ import annotations


def test_sync_prompt_default_targets_all_collections() -> None:
    from lies.mcp.prompts_impl import sync_prompt

    [msg] = sync_prompt()
    body = msg.text if hasattr(msg, "text") else str(msg)
    assert "lies sync" in body
    assert "Bash(lies sync --data-dir" in body


def test_sync_prompt_with_collections_passes_only_flag() -> None:
    from lies.mcp.prompts_impl import sync_prompt

    [msg] = sync_prompt(collections=["pydantic", "fastmcp"])
    body = msg.text if hasattr(msg, "text") else str(msg)
    assert "--only" in body
    assert "pydantic" in body
    assert "fastmcp" in body


def test_sync_prompt_with_dry_run_flag_passes_through() -> None:
    from lies.mcp.prompts_impl import sync_prompt

    [msg] = sync_prompt(dry_run=True)
    body = msg.text if hasattr(msg, "text") else str(msg)
    assert "--dry-run" in body


def test_sync_prompt_threads_scraper_timeout() -> None:
    from lies.mcp.prompts_impl import sync_prompt

    [msg] = sync_prompt(scraper_timeout=600)
    body = msg.text if hasattr(msg, "text") else str(msg)
    assert "--scraper-timeout 600" in body
