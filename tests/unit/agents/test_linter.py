"""Tests for the linter sub-agent (N2).

After the N2 rewire the linter's deps envelope is a marker type and
the system prompt carries the new dispatch instructions. These tests
pin the marker shape + the prompt's stop-when-saturated rule so a
regression to the pre-N2 prompt-stuffing envelope fails loudly.
"""

from __future__ import annotations

from dataclasses import is_dataclass

from lies.agents.linter import LINTER_SYSTEM_PROMPT, LintDeps


def test_lint_deps_is_marker_dataclass() -> None:
    """LintDeps is a dataclass with no fields (N2 marker type)."""
    assert is_dataclass(LintDeps)
    deps = LintDeps()
    # No fields beyond what dataclass adds; the agent's typed deps
    # surface stays narrow.
    assert len(deps.__dataclass_fields__) == 0


def test_linter_prompt_instructs_enumerate_first() -> None:
    """Prompt tells the linter to enumerate via wiki_list_pages first."""
    assert "wiki_list_pages" in LINTER_SYSTEM_PROMPT


def test_linter_prompt_instructs_batched_read() -> None:
    """Prompt carries the batched-read budget rule (≤10 pages)."""
    assert "wiki_read" in LINTER_SYSTEM_PROMPT
    assert "10" in LINTER_SYSTEM_PROMPT  # batch ceiling


def test_linter_prompt_instructs_stop_when_saturated() -> None:
    """Prompt carries the stop-when-saturated rule (budget discipline)."""
    lower = LINTER_SYSTEM_PROMPT.lower()
    assert "stop" in lower
    assert "saturat" in lower  # "saturated" or "saturation"
