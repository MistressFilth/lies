"""Pin the lint prompt routes through mcp__lies__lint."""

from __future__ import annotations

from tests.unit.mcp._prompt_body import rendered_body


def test_lint_prompt_default_routes_no_args() -> None:
    from lies.mcp.prompts_impl import lint_prompt

    [msg] = lint_prompt()
    body = rendered_body(msg)
    assert "mcp__lies__lint" in body
    assert "fix=False" in body


def test_lint_prompt_with_check_routes_check_name() -> None:
    from lies.mcp.prompts_impl import lint_prompt

    [msg] = lint_prompt(check="orphans")
    body = rendered_body(msg)
    assert "mcp__lies__lint" in body
    assert "orphans" in body


def test_lint_prompt_with_fix_carries_warning() -> None:
    from lies.mcp.prompts_impl import lint_prompt

    [msg] = lint_prompt(fix=True)
    body = rendered_body(msg)
    assert "fix=True" in body
    assert "repair" in body  # surface that the LLM should narrate repair outcomes
