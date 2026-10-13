"""The sync prompt hands the user's request to the agent verbatim, and the agent identifies collection names from natural language."""

from __future__ import annotations

from tests.unit.mcp._prompt_body import carries_verbatim, rendered_body


def test_sync_prompt_carries_the_user_request_verbatim() -> None:
    """The agent reads the user's request, not a tokenized positional list."""
    from lies.mcp.prompts_impl import sync_prompt

    [msg] = sync_prompt("pydantic fastmcp mermaid")
    body = rendered_body(msg)
    assert carries_verbatim(body, "pydantic fastmcp mermaid"), body
    assert "user_request" in body, body
    assert "Identify the collection names mentioned" in body, body


def test_sync_prompt_threads_the_flags_the_cli_has() -> None:
    """Flags are still parsed deterministically; only collection identification moves to the model."""
    from lies.mcp.prompts_impl import sync_prompt

    [msg] = sync_prompt("pydantic --force --skip-reindex --source https://x.example/docs")
    body = rendered_body(msg)
    assert "--force" in body
    assert "--skip-reindex" in body
    assert "--source https://x.example/docs" in body


def test_sync_prompt_instructs_for_general_requests() -> None:
    """A request that names no collection (or names them generally) leaves the positional off."""
    from lies.mcp.prompts_impl import sync_prompt

    [msg] = sync_prompt("sync the library")
    body = rendered_body(msg)
    assert "If the request is general" in body, body
    assert "Bash(lies sync)" in body or "`Bash(lies sync)`" in body, body


def test_sync_prompt_through_the_mcp_wire_shape() -> None:
    """The whole slash tail binds to the single `tail` string parameter, so a bare word can never land in a typed list[int] slot and fail JSON decode."""
    from lies.mcp.prompts_impl import sync_prompt

    [msg] = sync_prompt("pydantic --force --skip-reindex")
    body = rendered_body(msg)
    assert carries_verbatim(body, "pydantic --force --skip-reindex"), body
    assert "--force" in body
    assert "--skip-reindex" in body


def test_sync_value_flag_followed_by_a_flag_is_not_swallowed() -> None:
    """``--source --force`` set ``source='--force'`` and dropped ``--force`` entirely, so the body ran neither as asked."""
    from lies.mcp.prompts_impl import sync_prompt

    [msg] = sync_prompt("--source --force")
    body = rendered_body(msg)
    assert "Cannot run sync" in body
    assert "--source needs a value" in body
    assert "lies sync" not in body


def test_sync_value_flag_at_end_of_tail_is_reported() -> None:
    from lies.mcp.prompts_impl import sync_prompt

    [msg] = sync_prompt("pydantic --name")
    body = rendered_body(msg)
    assert "Cannot run sync" in body
    assert "--name needs a value" in body


def test_sync_names_an_unrecognized_flag() -> None:
    """An unrecognized flag is a refusal, not a dropped word."""
    from lies.mcp.prompts_impl import sync_prompt

    [msg] = sync_prompt("--bogus pydantic")
    body = rendered_body(msg)
    assert "Cannot run sync: 'pydantic' after --bogus" in body, body
    assert "Run Bash(" not in body, body


def test_sync_refuses_a_flag_with_no_value_behind_it() -> None:
    """``--bogus`` alone would leave the name list empty."""
    from lies.mcp.prompts_impl import sync_prompt

    for tail in ("--bogus", "-pydantic", "--data-dir"):
        [msg] = sync_prompt(tail)
        body = rendered_body(msg)
        assert body.startswith("Cannot run sync:"), f"{tail!r}:\n{body}"
        assert "Run Bash(" not in body, f"{tail!r}:\n{body}"


def test_the_removed_sync_flags_are_reported_not_rendered() -> None:
    """Retired flags are reported as retired, not rendered as if they worked."""
    from lies.mcp.prompts_impl import sync_prompt

    for invented, value in (
        ("--data-dir", "/tmp"),
        ("--only", "pydantic"),
        ("--jobs", "8"),
        ("--scraper-timeout", "600"),
        ("--no-ingest", ""),
        ("--dry-run", ""),
    ):
        [msg] = sync_prompt(f"{invented} {value}".strip())
        body = rendered_body(msg)
        assert invented in body, f"{invented} was accepted as if the CLI had it:\n{body}"
        assert body.startswith("Cannot run sync:"), body
        assert "Run Bash(" not in body, body


def test_sync_renders_the_negated_booleans_the_cli_declares() -> None:
    """A ``--no-<flag>`` the CLI declares is rendered."""
    from lies.mcp.prompts_impl import sync_prompt

    for tail, rendered in (
        ("pydantic --no-skip-reindex", "--no-skip-reindex"),
        ("pydantic --no-wait", "--no-wait"),
        ("pydantic --no-force", "--no-force"),
        ("pydantic --no-fail-busy", "--no-fail-busy"),
    ):
        [msg] = sync_prompt(tail)
        body = rendered_body(msg)
        assert rendered in body, f"{tail!r}: {body}"
        assert "Unrecognized flag(s)" not in body, body


def test_sync_no_positionals_pasted_english_does_not_fan_out() -> None:
    """Pasted English names no collection, so nothing fans out."""
    from lies.mcp.prompts_impl import sync_prompt

    [msg] = sync_prompt("please resync my library collections now")
    body = rendered_body(msg)
    # No pre-rendered commands — collection identification moved to
    # the agent. The verbatim request is in the body so the agent
    # can read it.
    assert "Run Bash(lies sync please)" not in body, body
    assert "Run Bash(lies sync resync)" not in body, body
    assert "Run Bash(lies sync collections)" not in body, body
    assert carries_verbatim(body, "please resync my library collections now"), body


def test_sync_asks_the_agent_to_ground_against_the_registry() -> None:
    """When a name is ambiguous, the agent queries ``collections_read`` rather than guessing."""
    from lies.mcp.prompts_impl import sync_prompt

    [msg] = sync_prompt("resync the thing I mentioned yesterday")
    body = rendered_body(msg)
    assert "mcp__lies__collections_read" in body, body
    assert "ask the user before dispatching" in body, body
