from __future__ import annotations

import os

import pytest

from pydantic_ai.models.test import TestModel

from lies.agents.linter import LintFinding, LintReport, linter_agent

pytestmark = pytest.mark.skipif(
    os.environ.get("CI") == "true",
    reason="TestModel queue exhaustion under full pytest tests/ run; see PR #98",
)


def test_linter_agent_exists() -> None:
    agent = linter_agent(model=TestModel())
    assert agent is not None


def test_linter_returns_report() -> None:
    agent = linter_agent(model=TestModel())
    with agent.override(model=TestModel()):
        result = agent.run_sync("Lint this wiki.")

    assert isinstance(result.output, LintReport)
    assert isinstance(result.output.findings, list)
    assert isinstance(result.output.report_markdown, str)
    for finding in result.output.findings:
        assert isinstance(finding, LintFinding)
