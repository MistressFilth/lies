"""Pin the ingest prompt routes sources through LIES ingest helpers."""

from __future__ import annotations

from tests.unit.mcp._prompt_body import rendered_body


def test_ingest_single_source_routes_to_bash_cli() -> None:
    from lies.mcp.prompts_impl import ingest_prompt

    [msg] = ingest_prompt("docs/pydantic.html")
    body = rendered_body(msg)
    assert "lies ingest" in body
    assert "docs/pydantic.html" in body


def test_ingest_delete_routes_to_bash_cli() -> None:
    from lies.mcp.prompts_impl import ingest_prompt

    [msg] = ingest_prompt("--delete entity-pydantic")
    body = rendered_body(msg)
    assert "delete" in body
    assert "entity-pydantic" in body


def test_ingest_batch_routes_to_bash_cli() -> None:
    from lies.mcp.prompts_impl import ingest_prompt

    [msg] = ingest_prompt("--batch /abs/docs")
    body = rendered_body(msg)
    assert "ingest --batch" in body
    assert "/abs/docs" in body


def test_ingest_delete_without_a_slug_says_so() -> None:
    """Regression: a bare ``--delete`` rendered the single-source branch
    with no source, producing a garbled ingest command rather than
    naming the missing value."""
    from lies.mcp.prompts_impl import ingest_prompt

    [msg] = ingest_prompt("--delete")
    body = rendered_body(msg)
    assert "Cannot run ingest" in body
    assert "--delete needs a value" in body
    assert "lies ingest" not in body


def test_ingest_path_with_an_apostrophe_is_quoted_not_split() -> None:
    """``shlex.split`` raised on the apostrophe; the surviving path is
    quoted so the shell the agent runs in sees one argument."""
    from lies.mcp.prompts_impl import ingest_prompt

    [msg] = ingest_prompt("docs/it's.html --type concept")
    body = rendered_body(msg)
    assert "--type concept" in body
    assert "docs/it'\"'\"'s.html" in body


def test_ingest_title_value_survives_as_one_argument() -> None:
    from lies.mcp.prompts_impl import ingest_prompt

    [msg] = ingest_prompt("docs/a.html --title two words --type concept")
    body = rendered_body(msg)
    assert "--title 'two words'" in body
