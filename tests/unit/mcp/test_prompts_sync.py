"""Pin the sync prompt routes through Bash(lies sync ...)."""

from __future__ import annotations

from tests.unit.mcp._prompt_body import rendered_body


def test_sync_prompt_default_targets_all_collections() -> None:
    from lies.mcp.prompts_impl import sync_prompt

    [msg] = sync_prompt("")
    body = rendered_body(msg)
    assert "lies sync" in body
    assert "Bash(lies sync --data-dir" in body


def test_sync_prompt_with_collections_passes_only_flag() -> None:
    from lies.mcp.prompts_impl import sync_prompt

    [msg] = sync_prompt("pydantic fastmcp")
    body = rendered_body(msg)
    assert "--only" in body
    assert "pydantic" in body
    assert "fastmcp" in body


def test_sync_prompt_with_dry_run_flag_passes_through() -> None:
    from lies.mcp.prompts_impl import sync_prompt

    [msg] = sync_prompt("--dry-run")
    body = rendered_body(msg)
    assert "--dry-run" in body


def test_sync_prompt_threads_scraper_timeout() -> None:
    from lies.mcp.prompts_impl import sync_prompt

    [msg] = sync_prompt("--scraper-timeout 600")
    body = rendered_body(msg)
    assert "--scraper-timeout 600" in body


def test_sync_prompt_all_marker_means_every_collection() -> None:
    """`/lies:sync all` and `/lies:sync --all` must not reach the CLI as a
    collection name. Both render a bare `lies sync`, which the CLI runs over
    every collection that has a scraper configured."""
    from lies.mcp.prompts_impl import sync_prompt

    for tail in ("all", "--all", "ALL", ""):
        [msg] = sync_prompt(tail)
        body = rendered_body(msg)
        assert "--only" not in body, f"{tail!r} leaked --only: {body!r}"
        assert 'Run Bash(lies sync --data-dir "$LIES_DATA")' in body


def test_sync_prompt_through_the_mcp_wire_shape() -> None:
    """The whole slash tail binds to the single `tail` string parameter, so a
    bare word can never land in a typed list[int] slot and fail JSON decode."""
    from lies.mcp.prompts_impl import sync_prompt

    [msg] = sync_prompt("pydantic fastmcp --force --jobs 8")
    body = rendered_body(msg)
    assert "--only pydantic fastmcp" in body
    assert "--force" in body
    assert "--jobs 8" in body


def test_sync_value_flag_followed_by_a_flag_is_not_swallowed() -> None:
    """Regression: ``--jobs --force`` set ``jobs='--force'`` and dropped
    ``--force`` entirely, so the body ran neither as asked."""
    from lies.mcp.prompts_impl import sync_prompt

    [msg] = sync_prompt("--jobs --force")
    body = rendered_body(msg)
    assert "Cannot run sync" in body
    assert "--jobs needs a value" in body
    assert "lies sync" not in body


def test_sync_value_flag_at_end_of_tail_is_reported() -> None:
    from lies.mcp.prompts_impl import sync_prompt

    [msg] = sync_prompt("pydantic --jobs")
    body = rendered_body(msg)
    assert "Cannot run sync" in body
    assert "--jobs needs a value" in body


def test_sync_names_an_unrecognized_flag() -> None:
    from lies.mcp.prompts_impl import sync_prompt

    [msg] = sync_prompt("--bogus pydantic")
    body = rendered_body(msg)
    assert "Unrecognized flag(s) ignored: --bogus." in body
    assert "--only pydantic" in body


def test_sync_quotes_a_collection_name_needing_it() -> None:
    """``--only`` is interpolated into a ``Bash()`` line, so a collection
    name containing shell metacharacters is re-quoted on the way out."""
    from lies.mcp.prompts_impl import sync_prompt

    [msg] = sync_prompt("a;b")
    assert "--only 'a;b'" in rendered_body(msg)
