"""Unit tests for repair_validation.validate_plan."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from lies.agents.linter import LintFinding, LintSeverity
from lies.agents.repair_models import RepairPlan
from lies.agents.repair_validation import (
    ValidatedRepairPlan,
    validate_plan,
)
from lies.memory.models import WikiPlanInvalid


@dataclass
class _FakeLayout:
    wiki_dir: Path

    def __init__(self, root: Path) -> None:
        self.wiki_dir = root / "wiki"


@pytest.fixture
def wiki_root(tmp_path: Path) -> _FakeLayout:
    (tmp_path / "wiki").mkdir(parents=True)
    (tmp_path / "wiki" / "index.md").write_text("# Index\n", encoding="utf-8")
    return _FakeLayout(tmp_path)


def test_validate_plan_empty_operations_returns_plan_unchanged(wiki_root: _FakeLayout) -> None:
    plan = RepairPlan(operations=[], rationale="noop", evidence=["f0"])
    findings: list[LintFinding] = []
    result = validate_plan(plan, wiki_root, findings)  # type: ignore[arg-type]
    assert isinstance(result, ValidatedRepairPlan)
    assert result.plan is plan
    assert result.dropped_ops == ()


def test_validate_plan_rejects_out_of_range_finding_index(wiki_root: _FakeLayout) -> None:
    from lies.agents.repair_models import CreateStub

    plan = RepairPlan(
        operations=[
            CreateStub(
                path="concepts/new.md",
                title="New",
                finding_index=5,  # no such finding
                pages=["concepts/new.md"],
                rationale="x",
                evidence=["f5"],
            ),
        ],
        rationale="r",
        evidence=["f5"],
    )
    findings = [
        LintFinding(
            severity=LintSeverity.LOW,
            category="missing_page",
            message="m",
            pages=["concepts/x.md"],
            safe_to_fix=True,
        ),
    ]
    with pytest.raises(WikiPlanInvalid, match="finding_index 5 out of range"):
        validate_plan(plan, wiki_root, findings)  # type: ignore[arg-type]


def test_validate_plan_rejects_unsafe_finding(wiki_root: _FakeLayout) -> None:
    from lies.agents.repair_models import CreateStub

    plan = RepairPlan(
        operations=[
            CreateStub(
                path="concepts/new.md",
                title="New",
                finding_index=0,
                pages=["concepts/new.md"],
                rationale="x",
                evidence=["f0"],
            ),
        ],
        rationale="r",
        evidence=["f0"],
    )
    findings = [
        LintFinding(
            severity=LintSeverity.HIGH,
            category="contradiction",
            message="c",
            pages=["concepts/x.md"],
            safe_to_fix=False,
        ),
    ]
    with pytest.raises(WikiPlanInvalid, match="safe_to_fix is False"):
        validate_plan(plan, wiki_root, findings)  # type: ignore[arg-type]
