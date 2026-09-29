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

    [msg] = ingest_prompt("--delete-arg", delete_slug="entity-pydantic")
    body = rendered_body(msg)
    assert "delete" in body
    assert "entity-pydantic" in body


def test_ingest_batch_routes_to_bash_cli() -> None:
    from lies.mcp.prompts_impl import ingest_prompt

    [msg] = ingest_prompt("--batch-arg", batch_dir="/abs/docs")
    body = rendered_body(msg)
    assert "ingest --batch" in body
    assert "/abs/docs" in body
