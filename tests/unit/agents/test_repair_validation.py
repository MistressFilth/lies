"""Unit tests for repair_validation.validate_plan."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from lies.agents.linter import LintFinding
from lies.agents.repair_models import RepairPlan
from lies.agents.repair_validation import (
    ValidatedRepairPlan,
    validate_plan,
)


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
