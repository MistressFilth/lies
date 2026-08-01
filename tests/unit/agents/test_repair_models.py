"""Unit tests for the RepairPlan data models."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from lies.agents.repair_models import (
    AppendEvidence,
    AppendLink,
    CreateStub,
    RepairPlan,
    RepairReceipt,
    UpdateIndex,
)
from lies.memory.models import OperationKind, PageReference


def test_repair_plan_is_noop_when_empty() -> None:
    plan = RepairPlan(operations=[], rationale="", evidence=["findings_present"])
    assert plan.is_noop() is True


def test_create_stub_requires_evidence() -> None:
    with pytest.raises(ValidationError):
        CreateStub(path="concepts/x.md", title="X", finding_index=0, pages=[], rationale="", evidence=[])


def test_append_link_requires_distinct_paths() -> None:
    with pytest.raises(ValidationError):
        AppendLink(
            target_path="concepts/a.md",
            link_text="A",
            append_to="concepts/a.md",
            finding_index=0,
            pages=["concepts/a.md"],
            rationale="self",
            evidence=["finding_1"],
        )


def test_update_index_path_must_be_index() -> None:
    with pytest.raises(ValidationError):
        UpdateIndex(
            path="wiki/overview.md",
            title="Overview",
            finding_index=0,
            pages=[],
            rationale="",
            evidence=["finding_1"],
        )


def test_append_evidence_requires_hash() -> None:
    with pytest.raises(ValidationError):
        AppendEvidence(
            path="concepts/x.md",
            expected_sha256="",
            content="## Note",
            finding_index=0,
            pages=["concepts/x.md"],
            rationale="",
            evidence=["finding_1"],
        )


def test_repair_plan_rejects_two_ops_on_same_path() -> None:
    plan = RepairPlan(
        operations=[
            CreateStub(path="concepts/x.md", title="X", finding_index=0, pages=[], rationale="a", evidence=["e1"]),
            UpdateIndex(path="wiki/index.md", title="X", finding_index=1, pages=[], rationale="b", evidence=["e1"]),
        ],
        rationale="two ops",
        evidence=["e1"],
    )
    assert plan.is_noop() is False
    with pytest.raises(ValidationError):
        RepairPlan(
            operations=[
                CreateStub(path="concepts/x.md", title="X", finding_index=0, pages=[], rationale="a", evidence=["e1"]),
                CreateStub(path="concepts/x.md", title="Y", finding_index=1, pages=[], rationale="b", evidence=["e1"]),
            ],
            rationale="dup",
            evidence=["e1"],
        )


def test_repair_plan_is_frozen() -> None:
    plan = RepairPlan(
        operations=[
            CreateStub(path="concepts/x.md", title="X", finding_index=0, pages=[], rationale="a", evidence=["e1"]),
        ],
        rationale="r",
        evidence=["e1"],
    )
    with pytest.raises(ValidationError):
        plan.rationale = "mutated"


def test_repair_receipt_carries_change_lists() -> None:
    receipt = RepairReceipt(
        applied=[
            PageReference(path="concepts/x.md", collection_id="wiki", op=OperationKind.CREATE)
        ],
        skipped=["finding_2: contradiction"],
        deferred=[],
        errors=[],
    )
    assert len(receipt.applied) == 1
    assert receipt.skipped == ["finding_2: contradiction"]
