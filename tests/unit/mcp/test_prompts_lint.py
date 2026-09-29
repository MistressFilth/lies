"""Pin the lint prompt routes through mcp__lies__lint."""

from __future__ import annotations


def test_lint_prompt_default_routes_no_args() -> None:
    from lies.mcp.prompts_impl import lint_prompt

    [msg] = lint_prompt()
    body = msg.text if hasattr(msg, "text") else str(msg)
    assert "mcp__lies__lint" in body
    assert "fix=False" in body


def test_lint_prompt_with_check_routes_check_name() -> None:
    from lies.mcp.prompts_impl import lint_prompt

    [msg] = lint_prompt(check="orphans")
    body = msg.text if hasattr(msg, "text") else str(msg)
    assert "mcp__lies__lint" in body
    assert "orphans" in body


def test_lint_prompt_with_fix_carries_warning() -> None:
    from lies.mcp.prompts_impl import lint_prompt

    [msg] = lint_prompt(fix=True)
    body = msg.text if hasattr(msg, "text") else str(msg)
    assert "fix=True" in body
    assert "repair" in body  # surface that the LLM should narrate repair outcomes
