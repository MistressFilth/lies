from dataclasses import is_dataclass

from lies.agents.linter import LintFinding, LintReport, LintSeverity


def test_lint_finding_is_dataclass() -> None:
    assert is_dataclass(LintFinding)
    f = LintFinding(
        severity=LintSeverity.HIGH,
        category="orphan",
        message="no inbound links",
        pages=["wiki/x.md"],
    )
    assert f.safe_to_fix is False


def test_lint_report_is_dataclass_with_list_of_findings() -> None:
    assert is_dataclass(LintReport)
    findings = [
        LintFinding(
            severity=LintSeverity.LOW,
            category="stale",
            message="x",
            pages=["wiki/y.md"],
        )
    ]
    r = LintReport(findings=findings, report_markdown="# report")
    assert len(r.findings) == 1
    assert r.report_markdown == "# report"


def test_linter_agent_output_type_accepts_dataclass() -> None:
    # Regression pin for pydantic-ai dataclass output_type.
    # NOTE: brief shows verbatim `linter_agent()` which requires
    # ANTHROPIC_API_KEY (the default model is `anthropic:claude-opus-4-7`).
    # CI does not set ANTHROPIC_API_KEY, so we pass TestModel() to keep the
    # construction assertion hermetic — the point of this pin is that the
    # agent *constructs* with dataclass deps/output, not that it talks to
    # Anthropic. Pattern matches tests/unit/agents/test_collection_author_dataclass_pin.py.
    from pydantic_ai.models.test import TestModel

    from lies.agents.linter import linter_agent

    agent = linter_agent(model=TestModel())
    assert agent is not None
