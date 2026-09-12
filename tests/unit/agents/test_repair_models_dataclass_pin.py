from dataclasses import is_dataclass

import pytest

from lies.agents.repair_models import CreateStub, RepairOpKind, RepairPlan


def test_create_stub_is_dataclass() -> None:
    assert is_dataclass(CreateStub)


def test_create_stub_min_length_on_evidence() -> None:
    with pytest.raises(ValueError, match="evidence"):
        CreateStub(
            finding_index=0,
            pages=[],
            rationale="r",
            evidence=[],
            path="wiki/x.md",
            title="x",
            kind=RepairOpKind.CREATE_STUB,
        )


def test_create_stub_valid_construction() -> None:
    s = CreateStub(
        finding_index=0,
        pages=["wiki/y.md"],
        rationale="r",
        evidence=["evidence"],
        path="wiki/x.md",
        title="x",
        kind=RepairOpKind.CREATE_STUB,
    )
    assert s.path == "wiki/x.md"
    assert s.kind == RepairOpKind.CREATE_STUB


def test_repair_plan_accepts_dataclass_create_stub_in_operations() -> None:
    plan = RepairPlan(
        operations=[
            CreateStub(
                finding_index=0,
                pages=["wiki/y.md"],
                rationale="r",
                evidence=["evidence"],
                path="wiki/x.md",
                title="x",
                kind=RepairOpKind.CREATE_STUB,
            )
        ],
        rationale="r",
        evidence=["evidence"],
    )
    assert len(plan.operations) == 1
    assert isinstance(plan.operations[0], CreateStub)
