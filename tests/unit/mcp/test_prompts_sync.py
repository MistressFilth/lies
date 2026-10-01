"""Pin the sync prompt hands the user's request to the agent verbatim,
and the agent identifies collection names from natural language.

The prompt's previous vocabulary (``--data-dir``, ``--only``,
``--jobs``, ``--scraper-timeout``, ``--no-ingest``, ``--dry-run``)
named options ``lies sync`` has never declared, so every one of those
commands exited 2 before it did anything. The vocabulary is
transcribed from the Typer signature in
``src/lies/cli/ingestion.py``; the flags that used to be advertised
are now *unrecognized*, and the tests below say so rather than
rendering them.

The shape matches ``ask_prompt`` and ``ground_prompt``: a verbatim
block carries the user's request; the agent interprets it. The body
does not parse collection names positionally, so a pasted sentence
("please resync my library collections") no longer fans out across
the library as one ``Run Bash(lies sync <word>)`` per word.
"""

from __future__ import annotations

from tests.unit.mcp._prompt_body import carries_verbatim, rendered_body


def test_sync_prompt_carries_the_user_request_verbatim() -> None:
    """The agent reads the user's request, not a tokenized positional list.

    A ``user_request`` fence appears, and the verbatim question text
    is inside it (no repr escaping). The agent uses
    ``mcp__lies__collections_read`` to ground names against the
    registry, so model-side identification — not the prompt body —
    decides which collections to dispatch.
    """
    from lies.mcp.prompts_impl import sync_prompt

    [msg] = sync_prompt("pydantic fastmcp mermaid")
    body = rendered_body(msg)
    assert carries_verbatim(body, "pydantic fastmcp mermaid"), body
    assert "user_request" in body, body
    assert "Identify the collection names mentioned" in body, body


def test_sync_prompt_threads_the_flags_the_cli_has() -> None:
    """Flags are still parsed deterministically; only collection
    identification moves to the model.
    """
    from lies.mcp.prompts_impl import sync_prompt

    [msg] = sync_prompt("pydantic --force --skip-reindex --source https://x.example/docs")
    body = rendered_body(msg)
    assert "--force" in body
    assert "--skip-reindex" in body
    assert "--source https://x.example/docs" in body


def test_sync_prompt_instructs_for_general_requests() -> None:
    """A request that names no collection (or names them generally)
    leaves the positional off — ``lies sync`` syncs every registered
    collection when no positional is given.
    """
    from lies.mcp.prompts_impl import sync_prompt

    [msg] = sync_prompt("sync the library")
    body = rendered_body(msg)
    assert "If the request is general" in body, body
    assert "Bash(lies sync)" in body or "`Bash(lies sync)`" in body, body


def test_sync_prompt_through_the_mcp_wire_shape() -> None:
    """The whole slash tail binds to the single `tail` string parameter, so a
    bare word can never land in a typed list[int] slot and fail JSON decode."""
    from lies.mcp.prompts_impl import sync_prompt

    [msg] = sync_prompt("pydantic --force --skip-reindex")
    body = rendered_body(msg)
    assert carries_verbatim(body, "pydantic --force --skip-reindex"), body
    assert "--force" in body
    assert "--skip-reindex" in body


def test_sync_value_flag_followed_by_a_flag_is_not_swallowed() -> None:
    """Regression: ``--source --force`` set ``source='--force'`` and dropped
    ``--force`` entirely, so the body ran neither as asked."""
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
    """An unrecognized flag is a refusal, not a dropped word.

    It used to be a note on a rendered command, which is the worst of
    both: ``sync --bogus pydantic`` rendered ``Bash(lies sync
    pydantic)`` *and* said "Unrecognized flag(s) ignored: --bogus", so
    the user could not tell whether the collection survived. Now the
    body refuses, and the refusal names both the flag and the word it
    would have swallowed.
    """
    from lies.mcp.prompts_impl import sync_prompt

    [msg] = sync_prompt("--bogus pydantic")
    body = rendered_body(msg)
    assert "Cannot run sync: 'pydantic' after --bogus" in body, body
    assert "Run Bash(" not in body, body


def test_sync_refuses_a_flag_with_no_value_behind_it() -> None:
    """``--bogus`` alone would leave the name list empty.

    Dropping the flag and rendering ``Bash(lies sync)`` is worse than
    rendering nothing: with no positional, ``lies sync`` syncs *every*
    registered collection. A typo would trigger a whole-library scrape
    and reindex. ``sync`` therefore refuses on any unrecognized flag.
    """
    from lies.mcp.prompts_impl import sync_prompt

    for tail in ("--bogus", "-pydantic", "--data-dir"):
        [msg] = sync_prompt(tail)
        body = rendered_body(msg)
        assert body.startswith("Cannot run sync:"), f"{tail!r}:\n{body}"
        assert "Run Bash(" not in body, f"{tail!r}:\n{body}"


def test_the_removed_sync_flags_are_reported_not_rendered() -> None:
    """The exact strings the review found, pinned as regressions.

    Each was advertised by the prompt and declared by no ``lies``
    command. If a future CLI grows one, this test fails and points at
    the prompt table that should offer it.
    """
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
    """``--no-skip-reindex`` was missing from the table, so the flag was
    dropped with no word and the rendered command ran the qmd
    update+embed chain the user asked to skip.

    With the natural-language handoff, the agent reads the flag and
    threads it into each ``Bash(lies sync ...)`` it dispatches. The
    flag is in the verbatim user request *and* threaded as a parsed
    flag, so the agent sees it twice (once raw, once structured) —
    the structured form is the one it must pass to Bash.
    """
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
    """A pasted sentence used to render one Bash per word.

    With no positional parsing, the agent reads the request and
    asks before dispatching. The body never pre-renders one Bash per
    word — collection identification belongs to the model.
    """
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
    """When a name is ambiguous, the agent queries ``collections_read``
    rather than guessing.
    """
    from lies.mcp.prompts_impl import sync_prompt

    [msg] = sync_prompt("resync the thing I mentioned yesterday")
    body = rendered_body(msg)
    assert "mcp__lies__collections_read" in body, body
    assert "ask the user before dispatching" in body, body
