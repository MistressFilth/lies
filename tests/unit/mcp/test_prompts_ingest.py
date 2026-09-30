"""Pin the ingest prompt routes sources through the real ``lies ingest``.

Every command in this file is a real one. The previous prompt rendered
``--data-dir "$LIES_DATA"``, ``--type``, and ``--delete`` — options
``lies ingest`` has never declared, so each of those commands exited 2
before it ingested anything. ``lies ingest`` takes ``--source`` or
``--batch`` and has no positional argument, so a bare path in the tail
becomes ``--source``.
"""

from __future__ import annotations

from tests.unit.mcp._prompt_body import rendered_body


def test_ingest_single_source_routes_to_bash_cli() -> None:
    from lies.mcp.prompts_impl import ingest_prompt

    [msg] = ingest_prompt("docs/pydantic.html")
    body = rendered_body(msg)
    assert "Run Bash(lies ingest --source docs/pydantic.html)" in body


def test_ingest_batch_routes_to_bash_cli() -> None:
    from lies.mcp.prompts_impl import ingest_prompt

    [msg] = ingest_prompt("--batch /abs/docs --slug-prefix mylib")
    body = rendered_body(msg)
    assert "ingest --batch /abs/docs" in body
    assert "--slug-prefix mylib" in body


def test_ingest_asks_rather_than_rendering_a_placeholder() -> None:
    """No source and no batch is a question, not a command.

    The old body rendered ``--source '<source>'`` — a command with a
    placeholder in it, which the agent would then run.
    """
    from lies.mcp.prompts_impl import ingest_prompt

    [msg] = ingest_prompt("")
    body = rendered_body(msg)
    assert "no source given" in body
    assert "Bash(" not in body


def test_ingest_refuses_two_sources_at_once() -> None:
    from lies.mcp.prompts_impl import ingest_prompt

    [msg] = ingest_prompt("docs/a.md --source docs/b.md")
    body = rendered_body(msg)
    assert "Cannot run ingest" in body
    assert "Bash(" not in body


def test_ingest_refuses_source_and_batch_together() -> None:
    from lies.mcp.prompts_impl import ingest_prompt

    [msg] = ingest_prompt("docs/a.md --batch docs/")
    body = rendered_body(msg)
    assert "Cannot run ingest" in body
    assert "Bash(" not in body


def test_ingest_delete_says_what_exists_instead_of_a_ghost_verb() -> None:
    """Nothing in the CLI removes an ingested page.

    The old body rendered ``lies ingest --delete <slug>``, which exits
    2. The remedy sentence names the two things that do exist and asks
    the user which they mean.
    """
    from lies.mcp.prompts_impl import ingest_prompt

    [msg] = ingest_prompt("--delete entity-pydantic")
    body = rendered_body(msg)
    assert "no CLI verb" in body
    assert "lies library delete" in body
    assert "--delete" not in body.split("Unrecognized flag(s)")[0].split("Bash(")[-1]
    assert "lies ingest --delete" not in body


def test_ingest_path_with_an_apostrophe_is_quoted_not_split() -> None:
    """``shlex.split`` raised on the apostrophe; the surviving path is
    quoted so the shell the agent runs in sees one argument."""
    from lies.mcp.prompts_impl import ingest_prompt

    [msg] = ingest_prompt("docs/it's.html --slug my-slug")
    body = rendered_body(msg)
    assert "docs/it'\"'\"'s.html" in body
    assert "--slug my-slug" in body


def test_ingest_title_value_survives_as_one_argument() -> None:
    from lies.mcp.prompts_impl import ingest_prompt

    [msg] = ingest_prompt("docs/a.html --title two words")
    body = rendered_body(msg)
    assert "--title 'two words'" in body


def test_ingest_renders_every_flag_the_cli_accepts() -> None:
    """A flag the CLI declares and the prompt drops is a silent loss.

    These four were parsed, then thrown away with no note: the value
    reached neither the command nor the parse-problem sentence.
    """
    from lies.mcp.prompts_impl import ingest_prompt

    [msg] = ingest_prompt(
        "--source docs/a.md --collection mylib --slug my-slug "
        "--exclude-stem _draft --exclude-dir node_modules"
    )
    body = rendered_body(msg)
    for rendered in (
        "--collection mylib",
        "--slug my-slug",
        "--exclude-stem _draft",
        "--exclude-dir node_modules",
    ):
        assert rendered in body, f"{rendered!r} silently dropped:\n{body}"


def test_ingest_value_flag_with_no_value_is_reported() -> None:
    from lies.mcp.prompts_impl import ingest_prompt

    [msg] = ingest_prompt("docs/a.md --slug")
    body = rendered_body(msg)
    assert "Cannot run ingest" in body
    assert "--slug needs a value" in body
    assert "Bash(" not in body


def test_ingest_reports_a_flag_the_cli_does_not_have() -> None:
    from lies.mcp.prompts_impl import ingest_prompt

    [msg] = ingest_prompt("docs/a.md --type concept")
    body = rendered_body(msg)
    assert "Unrecognized flag(s) ignored: --type." in body
    assert "--type" not in body.split("Unrecognized flag(s)")[0]


def test_ingest_explains_a_title_that_swallowed_the_source() -> None:
    """``--title`` takes every word up to the next flag, so a source
    typed after it is read as part of the title. The refusal then said
    "no source given" and pointed away from the cause."""
    from lies.mcp.prompts_impl import ingest_prompt

    [msg] = ingest_prompt('--title "Pydantic basics" x.md')
    body = rendered_body(msg)
    assert "no source given" in body
    assert "put the source first" in body


def test_ingest_renders_the_title_note_even_when_a_source_is_present() -> None:
    from lies.mcp.prompts_impl import ingest_prompt

    [msg] = ingest_prompt("--source docs/a.md --title Pydantic basics")
    body = rendered_body(msg)
    assert "Run Bash(lies ingest --source docs/a.md --title 'Pydantic basics')" in body
    assert "put the source first" in body
