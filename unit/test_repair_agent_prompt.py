"""Unit tests for the repair agent's system-prompt callable.

Regression for the C2 defect: pydantic-ai deps are host-side
``RunContext`` data, not auto-serialized into model messages. A
static ``system_prompt`` alone leaves the model without access to
the lint findings or the page bodies it needs to plan against.
The repair agent must register a system-prompt callable that
renders ``RepairAgentDeps`` (LintReport + page_texts) into the
prompt at run time.
"""

from __future__ import annotations

from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import RunContext

from lies.agents.linter import LintFinding, LintReport, LintSeverity
from lies.agents.repair import (
    RepairAgentDeps,
    _build_repair_prompt,
    repair_agent,
)


def _empty_report() -> LintReport:
    return LintReport(findings=[], report_markdown="")


def _report_with_orphan() -> LintReport:
    return LintReport(
        findings=[
            LintFinding(
                severity=LintSeverity.LOW,
                category="orphan",
                message="concepts/alpha.md has no inbound links.",
                pages=["concepts/alpha.md"],
                safe_to_fix=True,
            )
        ],
        report_markdown="",
    )


def test_build_repair_prompt_includes_lint_report_and_page_texts() -> None:
    """The repair agent's system-prompt callable must render the lint
    findings and page corpus into the prompt the model sees.
    """
    deps = RepairAgentDeps(
        lint_report=_report_with_orphan(),
        page_texts={
            "concepts/alpha.md": "---\ntitle: Alpha\n---\n# Alpha\n\nbody-A\n",
        },
    )
    ctx = RunContext(
        deps=deps,
        model=TestModel(),
        usage=None,  # type: ignore[arg-type]
        prompt="Propose a RepairPlan for the lint report.",
    )
    prompt = _build_repair_prompt(ctx)
    # Static instructions preserved.
    assert "HARD RULES" in prompt
    # Lint findings serialized as JSON.
    assert "orphan" in prompt
    assert "concepts/alpha.md" in prompt
    # Page text rendered.
    assert "body-A" in prompt
    assert "--- concepts/alpha.md ---" in prompt


def test_repair_agent_registers_dynamic_system_prompt() -> None:
    """The repair agent must register ``_build_repair_prompt`` as a
    system-prompt callable so pydantic-ai renders ``RepairAgentDeps``
    at run time. Without the registration, the model only sees the
    static prompt + the user message and cannot see the lint report.
    """
    agent: object = repair_agent(model=TestModel())
    sp_funcs = getattr(agent, "_system_prompt_functions", [])
    registered = [getattr(f, "function", f) for f in sp_funcs]
    assert _build_repair_prompt in registered, (
        f"_build_repair_prompt must be registered as a system-prompt callable, "
        f"got {[type(f).__name__ for f in registered]}"
    )


def test_build_repair_prompt_handles_empty_page_texts() -> None:
    """Empty page_texts still produces a valid prompt (lint report only)."""
    deps = RepairAgentDeps(lint_report=_empty_report(), page_texts={})
    ctx = RunContext(
        deps=deps,
        model=TestModel(),
        usage=None,  # type: ignore[arg-type]
        prompt="lint",
    )
    prompt = _build_repair_prompt(ctx)
    assert "HARD RULES" in prompt
    # Empty report still serializes cleanly.
    assert '"findings": []' in prompt
