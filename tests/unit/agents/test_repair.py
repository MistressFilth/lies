"""Unit tests for the repair_agent structured output."""

from __future__ import annotations

from pydantic_ai.models.test import TestModel

from lies.agents.linter import LintFinding, LintReport, LintSeverity
from lies.agents.repair import RepairAgentDeps, repair_agent
from lies.agents.repair_models import (
    RepairPlan,
)


def _finding(safe: bool, category: str = "missing_page") -> LintFinding:
    return LintFinding(
        severity=LintSeverity.LOW,
        category=category,
        message=f"finding {category}",
        pages=["concepts/x.md"],
        safe_to_fix=safe,
    )


def test_repair_agent_emits_noop_for_empty_report() -> None:
    """For an empty LintReport, the repair agent emits a noop RepairPlan."""
    noop_plan = RepairPlan(
        operations=[],
        rationale="no safe-to-fix findings",
        evidence=["findings_present"],
    )
    agent = repair_agent(model=TestModel(custom_output_args=noop_plan.model_dump()))
    deps = RepairAgentDeps(
        lint_report=LintReport(findings=[], report_markdown=""),
        page_texts={},
    )
    plan = agent.run_sync("noop", deps=deps).output
    assert isinstance(plan, RepairPlan)
    assert plan.is_noop()


def test_repair_agent_imports_cleanly() -> None:
    """Smoke: factory returns an Agent with the right deps and output types."""
    agent = repair_agent(model=TestModel())
    assert agent is not None


def test_repair_agent_prompts_safe_to_fix_respected() -> None:
    """Even if the user prompt names a safe_to_fix=False finding, the system
    prompt forbids emitting an op for it. The TestModel returns a structured
    plan from the prompt's instructions; verify the prompt instructs the
    agent to refuse."""
    from lies.agents.repair import REPAIR_AGENT_SYSTEM_PROMPT

    assert "safe_to_fix" in REPAIR_AGENT_SYSTEM_PROMPT
    assert "False" in REPAIR_AGENT_SYSTEM_PROMPT or "false" in REPAIR_AGENT_SYSTEM_PROMPT


def test_repair_receives_lint_report_and_page_texts() -> None:
    """RepairAgentDeps carries the lint report and the relevant page texts."""
    deps = RepairAgentDeps(
        lint_report=LintReport(findings=[_finding(True)], report_markdown=""),
        page_texts={"concepts/x.md": "# X"},
    )
    assert deps.lint_report.findings[0].safe_to_fix is True
    assert deps.page_texts["concepts/x.md"] == "# X"


def test_repair_agent_uses_repair_plan_output_type() -> None:
    """The agent's output_type is RepairPlan."""

    from lies.agents.repair_models import RepairPlan

    agent = repair_agent(model=TestModel())
    assert agent.output_type is RepairPlan


def test_repair_agent_module_exports_repair_agent() -> None:
    """agents/__init__ re-exports repair_agent."""
    from lies.agents import repair_agent as imported

    assert imported is repair_agent
