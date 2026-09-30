"""Pin the sync prompt routes through Bash(lies sync ...).

Every command in this file is a real one. The prompt's previous
vocabulary (``--data-dir``, ``--only``, ``--jobs``,
``--scraper-timeout``, ``--no-ingest``, ``--dry-run``) named options
``lies sync`` has never declared, so every one of these commands exited
2 before it did anything. The vocabulary is transcribed from the Typer
signature in ``src/lies/cli/ingestion.py``; the flags that used to be
advertised are now *unrecognized*, and the tests below say so rather
than rendering them.
"""

from __future__ import annotations

from tests.unit.mcp._prompt_body import rendered_body


def test_sync_prompt_default_targets_all_collections() -> None:
    from lies.mcp.prompts_impl import sync_prompt

    [msg] = sync_prompt("")
    body = rendered_body(msg)
    assert "Run Bash(lies sync)" in body
    # Every option the old body invented, gone.
    assert "--data-dir" not in body
    assert "LIES_DATA" not in body


def test_sync_prompt_one_command_per_collection() -> None:
    """``lies sync`` takes one positional, so two names are two commands.

    Splicing them into a list-valued flag was how ``sync --jbos 8``
    rendered ``--only 8``: the unrecognized flag's value became a
    collection name nobody typed.
    """
    from lies.mcp.prompts_impl import sync_prompt

    [msg] = sync_prompt("pydantic fastmcp")
    body = rendered_body(msg)
    assert "Run Bash(lies sync pydantic); Run Bash(lies sync fastmcp)" in body
    assert "--only" not in body
    # Every named collection became a command, so none is left over to
    # report. A leftover note here contradicted the commands above it.
    assert "Not consumed by" not in body


def test_sync_prompt_threads_the_flags_the_cli_has() -> None:
    from lies.mcp.prompts_impl import sync_prompt

    [msg] = sync_prompt("pydantic --force --skip-reindex --source https://x.example/docs")
    body = rendered_body(msg)
    assert "--force" in body
    assert "--skip-reindex" in body
    assert "--source https://x.example/docs" in body


def test_sync_prompt_all_marker_means_every_collection() -> None:
    """`/lies:sync all` must not reach the CLI as a collection name.

    A bare ``lies sync`` is what the CLI runs over every collection
    that has a scraper configured.
    """
    from lies.mcp.prompts_impl import sync_prompt

    for tail in ("all", "ALL", ""):
        [msg] = sync_prompt(tail)
        body = rendered_body(msg)
        assert "Run Bash(lies sync)" in body, f"{tail!r}: {body!r}"


def test_sync_prompt_through_the_mcp_wire_shape() -> None:
    """The whole slash tail binds to the single `tail` string parameter, so a
    bare word can never land in a typed list[int] slot and fail JSON decode."""
    from lies.mcp.prompts_impl import sync_prompt

    [msg] = sync_prompt("pydantic --force --skip-reindex")
    body = rendered_body(msg)
    assert "Run Bash(lies sync pydantic" in body
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
        # The old assertion searched for "Run Bash(lies sync--data-dir"
        # -- no space, so it was a string no body can produce and it
        # never fired. The check is now that no command renders.
        assert "Run Bash(" not in body, body


def test_sync_quotes_a_collection_name_needing_it() -> None:
    """A collection name is interpolated into a ``Bash()`` line, so one
    containing shell metacharacters is re-quoted on the way out."""
    from lies.mcp.prompts_impl import sync_prompt

    [msg] = sync_prompt("a;b")
    assert "Run Bash(lies sync 'a;b')" in rendered_body(msg)


def test_sync_renders_the_negated_booleans_the_cli_declares() -> None:
    """``--no-skip-reindex`` was missing from the table, so the flag was
    dropped with no word and the rendered command ran the qmd
    update+embed chain the user asked to skip."""
    from lies.mcp.prompts_impl import sync_prompt

    for tail, rendered in (
        ("pydantic --no-skip-reindex", "--no-skip-reindex"),
        ("pydantic --no-wait", "--no-wait"),
        ("pydantic --no-force", "--no-force"),
        ("pydantic --no-fail-busy", "--no-fail-busy"),
    ):
        [msg] = sync_prompt(tail)
        body = rendered_body(msg)
        assert f"Run Bash(lies sync pydantic {rendered})" in body, body
        assert "Unrecognized flag(s)" not in body, body


def test_sync_refuses_to_fan_out_over_a_pasted_sentence() -> None:
    """One command per word is one scrape-and-reindex chain per word.

    ``lies sync`` takes a single positional, so "please resync my
    library collections" rendered five invocations — and the sentence
    justifying them read as a claim about a request the user never
    made. Above the cap the body refuses and names the words.
    """
    from lies.mcp.prompts_impl import sync_prompt

    [msg] = sync_prompt("please resync my library collections now")
    body = rendered_body(msg)
    assert body.startswith("Cannot run sync:"), body
    assert "Run Bash(" not in body, body

    # A genuine multi-collection request still renders, and says how
    # many invocations it fires.
    [msg] = sync_prompt("pydantic fastmcp mermaid")
    body = rendered_body(msg)
    assert "names 3" in body, body
    assert body.count("Run Bash(") == 3, body
