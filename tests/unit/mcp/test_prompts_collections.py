"""Pin the collections prompt routes subcommands through LIES surfaces."""

from __future__ import annotations


def test_collections_list_routes_to_collections_read() -> None:
    from lies.mcp.prompts_impl import collections_prompt

    [msg] = collections_prompt("list")
    body = msg.text if hasattr(msg, "text") else str(msg)
    assert "mcp__lies__collections_read" in body
    assert "list" in body


def test_collections_add_routes_to_bash_cli() -> None:
    from lies.mcp.prompts_impl import collections_prompt

    [msg] = collections_prompt("add", args=["mylib", "/abs/path"])
    body = msg.text if hasattr(msg, "text") else str(msg)
    assert "lies library" in body
    assert "mylib" in body


def test_collections_unknown_subcommand_lists_options() -> None:
    from lies.mcp.prompts_impl import collections_prompt

    [msg] = collections_prompt("unknown_sub")
    body = msg.text if hasattr(msg, "text") else str(msg)
    assert "list" in body
    assert "add" in body
