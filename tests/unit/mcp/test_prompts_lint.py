"""Pin the lint prompt routes through mcp__lies__lint."""

from __future__ import annotations

from tests.unit.mcp._prompt_body import rendered_body


def test_lint_prompt_default_routes_no_args() -> None:
    from lies.mcp.prompts_impl import lint_prompt

    [msg] = lint_prompt("")
    body = rendered_body(msg)
    assert "mcp__lies__lint" in body
    assert "fix=False" in body


def test_lint_prompt_with_check_routes_check_name() -> None:
    from lies.mcp.prompts_impl import lint_prompt

    [msg] = lint_prompt("--check orphans")
    body = rendered_body(msg)
    assert "mcp__lies__lint" in body
    assert "orphans" in body


def test_lint_prompt_with_fix_carries_warning() -> None:
    from lies.mcp.prompts_impl import lint_prompt

    [msg] = lint_prompt("--fix")
    body = rendered_body(msg)
    assert "fix=True" in body
    assert "repair" in body  # surface that the LLM should narrate repair outcomes


def test_lint_missing_check_value_reports_instead_of_running_unfiltered() -> None:
    """A value flag with no value used to be dropped, so the caller asked
    for a scoped lint and got the full one with no indication."""
    from lies.mcp.prompts_impl import lint_prompt

    [msg] = lint_prompt("--check")
    body = rendered_body(msg)
    assert "Cannot run lint" in body
    assert "--check needs a value" in body
    assert "mcp__lies__lint" not in body


def test_lint_check_flag_falls_back_to_the_equals_form_only_when_unparsable() -> None:
    from lies.mcp.prompts_impl import lint_prompt

    for tail in ("--check orphans", "--check=orphans"):
        [msg] = lint_prompt(tail)
        assert "check='orphans'" in rendered_body(msg)


def test_lint_combines_check_and_fix() -> None:
    from lies.mcp.prompts_impl import lint_prompt

    [msg] = lint_prompt("--check orphans --fix")
    body = rendered_body(msg)
    assert "check='orphans'" in body
    assert "fix=True" in body
    assert "repair" in body


def test_lint_names_an_unrecognized_flag() -> None:
    from lies.mcp.prompts_impl import lint_prompt

    [msg] = lint_prompt("--fixx --check orphans")
    assert "Unrecognized flag(s) ignored: --fixx." in rendered_body(msg)
