"""Pin the collections prompt routes subcommands through LIES surfaces."""

from __future__ import annotations

from lies.mcp.prompts_impl import collections_prompt
from tests.unit.mcp._prompt_body import rendered_body


def test_collections_list_routes_to_collections_read() -> None:
    [msg] = collections_prompt("list")
    body = rendered_body(msg)
    assert "mcp__lies__collections_read" in body
    assert "list" in body


def test_collections_add_routes_to_bash_cli() -> None:
    [msg] = collections_prompt("add mylib /abs/path")
    body = rendered_body(msg)
    assert "lies library" in body
    assert "mylib" in body


def test_collections_unknown_subcommand_lists_options() -> None:
    [msg] = collections_prompt("unknown_sub")
    body = rendered_body(msg)
    assert "list" in body
    assert "add" in body
