"""Regression for the 2f320888 wrong-path bug.

Session 2f320888-dd1e-422e-8562-1f5d4504b952 launched from
``cwd=~/`` with no AGENTS.md in scope, then picked
``wiki/<collection>/<page-type>/<slug>.md`` for "where do
ingested collections live". That path is the ``lies page write``
target, not the source-collection storage location. The
orientation payload must surface the right paths and never
surface the wrong one.

This test pins both halves of that contract:

- Right path: ``$XDG_CONFIG_HOME/lies/<wiki>/collections/``
  appears in the orientation text.
- Wrong path: ``wiki/<collection>/<page-type>/<slug>.md``
  does NOT appear in the orientation text.

Model-side reasoning ("would the agent pick the right path")
is out of scope for the automated gate; manual replay of one
session per release covers it.
"""

from __future__ import annotations

import pytest

from lies.mcp.instructions_loader import (
    load_instructions,
    load_prompt,
)

# The wrong path the bug picked. page-write target, not collection storage.
WRONG_PATH = "wiki/<collection>/<page-type>/<slug>.md"

# The right paths the orientation payload must surface.
RIGHT_PATHS = (
    "$XDG_CONFIG_HOME/lies/<wiki>/collections/",
    "$XDG_DATA_HOME/lies/<wiki>/wiki/",
    "$XDG_DATA_HOME/lies/<wiki>/raw/<collection>/",
)


def _orientation_text() -> str:
    """Concatenate the handshake + the root orient prompt body."""
    return load_instructions() + "\n\n" + load_prompt("orient")


@pytest.mark.parametrize("right_path", RIGHT_PATHS)
def test_orientation_carries_right_path(right_path: str) -> None:
    text = _orientation_text()
    assert right_path in text, f"orientation payload missing {right_path!r}"


def test_orientation_does_not_carry_wrong_path() -> None:
    text = _orientation_text()
    assert WRONG_PATH not in text, (
        f"orientation payload carries the wrong path {WRONG_PATH!r}; "
        "this is the path the 2f320888 bug picked (page-write target, "
        "not source-collection storage). Remove from instructions.md "
        "and orient.md."
    )
