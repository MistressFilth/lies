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

    @property
    def index_path(self) -> Path:
        return self.wiki_dir / "index.md"


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


def test_validate_plan_rejects_op_pages_outside_finding(wiki_root: _FakeLayout) -> None:
    from lies.agents.repair_models import CreateStub

    plan = RepairPlan(
        operations=[
            CreateStub(
                path="concepts/new.md",
                title="New",
                finding_index=0,
                pages=["concepts/other.md"],  # not in finding.pages
                rationale="x",
                evidence=["f0"],
            ),
        ],
        rationale="r",
        evidence=["f0"],
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
    with pytest.raises(WikiPlanInvalid, match="do not intersect finding"):
        validate_plan(plan, wiki_root, findings)  # type: ignore[arg-type]


def test_validate_plan_rejects_create_stub_on_existing_path(wiki_root: _FakeLayout) -> None:
    from lies.agents.repair_models import CreateStub

    existing = wiki_root.wiki_dir / "concepts" / "taken.md"
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_text("existing", encoding="utf-8")

    plan = RepairPlan(
        operations=[
            CreateStub(
                path="concepts/taken.md",
                title="Taken",
                finding_index=0,
                pages=["concepts/taken.md"],
                rationale="x",
                evidence=["f0"],
            ),
        ],
        rationale="r",
        evidence=["f0"],
    )
    findings = [
        LintFinding(
            severity=LintSeverity.LOW,
            category="missing_page",
            message="m",
            pages=["concepts/taken.md"],
            safe_to_fix=True,
        ),
    ]
    with pytest.raises(WikiPlanInvalid, match="already exists"):
        validate_plan(plan, wiki_root, findings)  # type: ignore[arg-type]


def test_validate_plan_rejects_append_link_to_missing_target(wiki_root: _FakeLayout) -> None:
    from lies.agents.repair_models import AppendLink

    host = wiki_root.wiki_dir / "concepts" / "a.md"
    host.parent.mkdir(parents=True, exist_ok=True)
    host.write_text("---\ntitle: A\n---\n# A\n", encoding="utf-8")

    plan = RepairPlan(
        operations=[
            AppendLink(
                target_path="concepts/missing.md",
                link_text="Missing",
                append_to="concepts/a.md",
                finding_index=0,
                pages=["concepts/a.md"],
                rationale="x",
                evidence=["f0"],
            ),
        ],
        rationale="r",
        evidence=["f0"],
    )
    findings = [
        LintFinding(
            severity=LintSeverity.MEDIUM,
            category="missing_xref",
            message="m",
            pages=["concepts/a.md"],
            safe_to_fix=True,
        ),
    ]
    with pytest.raises(WikiPlanInvalid, match="does not exist; use CreateStub"):
        validate_plan(plan, wiki_root, findings)  # type: ignore[arg-type]


def test_validate_plan_rejects_append_link_with_missing_append_to(wiki_root: _FakeLayout) -> None:
    from lies.agents.repair_models import AppendLink

    target = wiki_root.wiki_dir / "concepts" / "b.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# B\n", encoding="utf-8")

    plan = RepairPlan(
        operations=[
            AppendLink(
                target_path="concepts/b.md",
                link_text="B",
                append_to="concepts/ghost.md",  # does not exist
                finding_index=0,
                pages=["concepts/ghost.md"],
                rationale="x",
                evidence=["f0"],
            ),
        ],
        rationale="r",
        evidence=["f0"],
    )
    findings = [
        LintFinding(
            severity=LintSeverity.MEDIUM,
            category="missing_xref",
            message="m",
            pages=["concepts/ghost.md"],
            safe_to_fix=True,
        ),
    ]
    with pytest.raises(WikiPlanInvalid, match=r"append_to .* does not exist"):
        validate_plan(plan, wiki_root, findings)  # type: ignore[arg-type]


def test_validate_plan_rejects_append_evidence_on_missing_path(wiki_root: _FakeLayout) -> None:
    from lies.agents.repair_models import AppendEvidence

    plan = RepairPlan(
        operations=[
            AppendEvidence(
                path="concepts/none.md",
                expected_sha256="0" * 64,
                content="evidence",
                finding_index=0,
                pages=["concepts/none.md"],
                rationale="x",
                evidence=["f0"],
            ),
        ],
        rationale="r",
        evidence=["f0"],
    )
    findings = [
        LintFinding(
            severity=LintSeverity.MEDIUM,
            category="missing_xref",
            message="m",
            pages=["concepts/none.md"],
            safe_to_fix=True,
        ),
    ]
    with pytest.raises(WikiPlanInvalid, match=r"AppendEvidence: path .* does not exist"):
        validate_plan(plan, wiki_root, findings)  # type: ignore[arg-type]


def test_validate_plan_accepts_safe_create_stub_with_no_drops(wiki_root: _FakeLayout) -> None:
    from lies.agents.repair_models import CreateStub

    plan = RepairPlan(
        operations=[
            CreateStub(
                path="concepts/brand-new.md",
                title="Brand New",
                finding_index=0,
                pages=["concepts/brand-new.md"],
                rationale="y",
                evidence=["f0"],
            ),
        ],
        rationale="r",
        evidence=["f0"],
    )
    findings = [
        LintFinding(
            severity=LintSeverity.LOW,
            category="missing_page",
            message="m",
            pages=["concepts/brand-new.md"],
            safe_to_fix=True,
        ),
    ]
    result = validate_plan(plan, wiki_root, findings)  # type: ignore[arg-type]
    assert isinstance(result, ValidatedRepairPlan)
    assert result.dropped_ops == ()
    assert result.plan is plan
    assert result.plan.operations == plan.operations


def test_validate_plan_drops_redundant_update_index(wiki_root: _FakeLayout) -> None:
    from lies.agents.repair_models import CreateStub, UpdateIndex

    # Seed an index that already lists the orphan.
    (wiki_root.wiki_dir / "concepts").mkdir(parents=True, exist_ok=True)
    (wiki_root.wiki_dir / "index.md").write_text(
        "# Index\n- [Already](concepts/already.md)\n", encoding="utf-8"
    )

    plan = RepairPlan(
        operations=[
            UpdateIndex(
                path="wiki/index.md",
                title="Already",
                finding_index=0,
                pages=["concepts/already.md"],
                rationale="x",
                evidence=["f0"],
            ),
            CreateStub(
                path="concepts/brand-new.md",
                title="Brand New",
                finding_index=0,
                pages=["concepts/brand-new.md"],
                rationale="y",
                evidence=["f0"],
            ),
        ],
        rationale="r",
        evidence=["f0"],
    )
    findings = [
        LintFinding(
            severity=LintSeverity.LOW,
            category="orphan",
            message="m",
            pages=["concepts/already.md", "concepts/brand-new.md"],
            safe_to_fix=True,
        ),
    ]
    result = validate_plan(plan, wiki_root, findings)  # type: ignore[arg-type]
    assert result.dropped_ops == (0,)
    assert len(result.plan.operations) == 1
    assert isinstance(result.plan.operations[0], CreateStub)
    assert result.plan.operations[0].path == "concepts/brand-new.md"
