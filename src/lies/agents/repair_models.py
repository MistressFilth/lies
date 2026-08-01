"""Pydantic models for the lint-repair workflow.

RepairPlan is the structured output of the repair_agent. The 4
primitives (CreateStub, AppendLink, UpdateIndex, AppendEvidence) map
1:1 onto existing WikiMemoryService operations. Operations on the
same path are rejected; the apply-plan envelope is single-commit.
"""
from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from lies.memory.models import PageReference


class RepairOpKind(str, Enum):
    CREATE_STUB = "create_stub"
    APPEND_LINK = "append_link"
    UPDATE_INDEX = "update_index"
    APPEND_EVIDENCE = "append_evidence"


class _RepairOp(BaseModel):
    """Base for repair operations."""

    model_config = ConfigDict(frozen=True)

    finding_index: int
    pages: list[str] = Field(default_factory=list)
    rationale: str
    evidence: list[str] = Field(min_length=1)


class CreateStub(_RepairOp):
    """Create a stub page for a missing entity or concept."""

    path: str
    title: str
    kind: Literal[RepairOpKind.CREATE_STUB] = RepairOpKind.CREATE_STUB


class AppendLink(_RepairOp):
    """Append a markdown link to an existing page."""

    target_path: str
    link_text: str
    anchor: str = ""
    append_to: str
    kind: Literal[RepairOpKind.APPEND_LINK] = RepairOpKind.APPEND_LINK

    @model_validator(mode="after")
    def _distinct_paths(self) -> AppendLink:
        if self.target_path == self.append_to:
            raise ValueError(
                f"AppendLink: target_path must differ from append_to "
                f"({self.append_to!r})"
            )
        return self

    @property
    def path(self) -> str:
        return self.append_to


class UpdateIndex(_RepairOp):
    """Add an entry to wiki/index.md."""

    path: str
    title: str
    kind: Literal[RepairOpKind.UPDATE_INDEX] = RepairOpKind.UPDATE_INDEX

    @model_validator(mode="after")
    def _path_must_be_index(self) -> UpdateIndex:
        if self.path != "wiki/index.md":
            raise ValueError(
                f"UpdateIndex.path must be 'wiki/index.md', got {self.path!r}"
            )
        return self


class AppendEvidence(_RepairOp):
    """Append a short evidence block to an existing page."""

    path: str
    expected_sha256: str = Field(min_length=1)
    content: str
    kind: Literal[RepairOpKind.APPEND_EVIDENCE] = RepairOpKind.APPEND_EVIDENCE


class RepairPlan(BaseModel):
    """A structured set of repair operations proposed by repair_agent."""

    model_config = ConfigDict(frozen=True)

    operations: list[_RepairOp]
    rationale: str
    evidence: list[str] = Field(min_length=1)

    def is_noop(self) -> bool:
        return not self.operations

    @model_validator(mode="after")
    def _no_conflicting_ops(self) -> RepairPlan:
        seen: dict[str, str] = {}
        for op in self.operations:
            path = op.path  # type: ignore[attr-defined]
            kind = type(op).__name__
            prev = seen.get(path)
            if prev is not None and prev != "AppendLink":
                raise ValueError(f"multiple operations target the same path: {path}")
            if prev is not None and kind != "AppendLink":
                raise ValueError(f"cannot mix AppendLink with another op on the same path: {path}")
            seen[path] = kind
        return self


class RepairReceipt(BaseModel):
    """Result of applying (or attempting to apply) a RepairPlan."""

    model_config = ConfigDict(frozen=True)

    applied: list[PageReference] = Field(default_factory=list)
    applied_repair_kinds: list[str] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)
    deferred: list[str] = Field(default_factory=list)
    fallback_used: bool = False
    fallback_reason: str = ""
    errors: list[str] = Field(default_factory=list)
