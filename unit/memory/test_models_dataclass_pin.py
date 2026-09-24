from dataclasses import is_dataclass

from lies.memory.models import (
    EvidenceAppend,
    MemoryPlan,
    OperationKind,
    PageCreate,
    PageDelete,
    PageUpdate,
)


def test_page_create_is_dataclass() -> None:
    assert is_dataclass(PageCreate)


def test_page_delete_is_dataclass() -> None:
    assert is_dataclass(PageDelete)


def test_page_update_remains_base_model() -> None:
    # Sibling with Field(min_length=1) — not flagged, must stay BaseModel.
    from pydantic import BaseModel

    assert issubclass(PageUpdate, BaseModel)


def test_evidence_append_remains_base_model() -> None:
    from pydantic import BaseModel

    assert issubclass(EvidenceAppend, BaseModel)


def test_memory_plan_with_create_and_delete_ops() -> None:
    plan = MemoryPlan(
        rationale="test",
        evidence=["e"],
        operations=[
            PageCreate(path="a.md", content="# new", evidence=["a-ev"], kind=OperationKind.CREATE),
            PageDelete(path="b.md", evidence=["b-ev"], kind=OperationKind.DELETE),
        ],
    )
    kinds = [type(op).__name__ for op in plan.operations]
    assert kinds == ["PageCreate", "PageDelete"]
