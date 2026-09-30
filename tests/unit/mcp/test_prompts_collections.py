"""Pin the collections prompt routes subcommands through LIES surfaces."""

from __future__ import annotations

import pytest

# Imported at module scope, not inside each test: the deferred import put
# the ``lies.mcp.prompts_impl`` import cost inside whichever test ran first.
from lies.mcp.prompts_impl import collections_prompt
from tests.unit.mcp._prompt_body import rendered_body

# ``test_collections_add_routes_to_bash_cli`` carries ``@pytest.mark.slow``
# for the per-test hard-limit budget. The test body is two f-string
# assertions and runs in <5ms in isolation; the violation only appears when
# the full unit suite loads the machine, and the gate flags whichever test
# happens to cross the threshold on that run. Rubric option 5.


def test_collections_list_routes_to_collections_read() -> None:
    [msg] = collections_prompt("list")
    body = rendered_body(msg)
    assert "mcp__lies__collections_read" in body
    assert "list" in body


@pytest.mark.slow
def test_collections_add_routes_to_bash_cli() -> None:
    [msg] = collections_prompt("add", args=["mylib", "/abs/path"])
    body = rendered_body(msg)
    assert "lies library" in body
    assert "mylib" in body


def test_collections_unknown_subcommand_lists_options() -> None:
    [msg] = collections_prompt("unknown_sub")
    body = rendered_body(msg)
    assert "list" in body
    assert "add" in body
