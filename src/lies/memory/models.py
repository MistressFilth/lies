"""Pydantic models for wiki memory.

The models are the public contract used by the Pydantic AI read tools,
the MemoryEnricher structured output, the WikiMemoryService, and the
FastMCP adapter.
"""

from __future__ import annotations

from enum import Enum
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Re-exported from :mod:`lies.lock_errors` so the historical import path
# (``from lies.memory.models import WikiLockBusy``) keeps working for
# existing callers (service.py, retry.py, orchestrator.py, tests). The
# class now subclasses :class:`lies.lock_errors.WikiFlockError` instead
# of :class:`WikiMemoryError` — see Task 3 of the v0.10.3-flocks spec.
from lies.lock_errors import WikiLockBusy  # noqa: F401

# --- Errors -------------------------------------------------------------


class WikiMemoryError(Exception):
    """Base class for typed WikiMemoryService failures."""


class WikiSearchUnavailable(WikiMemoryError):
    """qmd and the index fallback both failed or returned nothing."""


class WikiPageNotFound(WikiMemoryError):
    """Requested page_id was not returned by a recent search."""


class WikiPlanInvalid(WikiMemoryError):
    """A MemoryPlan failed validation. Carries the failing path when known."""

    def __init__(self, reason: str, *, path: str | None = None) -> None:
        super().__init__(reason)
        self.path = path


class WikiEvidenceMissing(WikiPlanInvalid):
    """A MemoryPlan operation lacked required evidence references."""


class WikiWriteConflict(WikiMemoryError):
    """A page's expected_sha256 did not match the current content."""


class WikiCommitFailed(WikiMemoryError):
    """The atomic commit step failed and the wiki was rolled back."""


class WikiIndexStale(WikiMemoryError):
    """The git commit succeeded but the qmd derived index is stale."""


class WikiCollectionInvalid(WikiMemoryError):
    """The referenced collection is not registered with the service."""


# --- Collection and evidence -------------------------------------------


class WikiCollectionRef(BaseModel):
    """A reference to a prepared wiki collection."""

    model_config = ConfigDict(frozen=True)

    collection_id: str
    root: PurePosixPath
    qmd_collection: str
    schema_path: PurePosixPath


class WikiEvidence(BaseModel):
    """A bounded excerpt from a wiki page, returned by `wiki_search`."""

    model_config = ConfigDict(frozen=True)

    page_id: str
    path: str
    collection_id: str
    excerpt: str
    line_start: int = Field(ge=0)
    line_end: int = Field(ge=0)
    score: float = Field(ge=0.0, le=1.0)


class WikiSearchResult(BaseModel):
    """A bounded set of evidence from a wiki search."""

    model_config = ConfigDict(frozen=True)

    query: str
    pages: list[WikiEvidence]
    truncated: bool
    fallback_used: bool
    fallback_reason: str


class PageReference(BaseModel):
    """A reference to a page that was read or changed."""

    model_config = ConfigDict(frozen=True)

    path: str
    collection_id: str
    op: OperationKind


# --- Memory plan --------------------------------------------------------


class OperationKind(str, Enum):
    CREATE = "create"
    UPDATE = "update"
    APPEND = "append"


class _PlanOperation(BaseModel):
    """Base for plan operations."""

    model_config = ConfigDict(frozen=True)

    path: str
    evidence: list[str] = Field(min_length=1)

    kind: OperationKind


class PageCreate(_PlanOperation):
    """Create a new wiki page."""

    content: str
    kind: Literal[OperationKind.CREATE] = OperationKind.CREATE


class PageUpdate(_PlanOperation):
    """Replace a wiki page with a versioned update."""

    expected_sha256: str = Field(min_length=1)
    content: str
    kind: Literal[OperationKind.UPDATE] = OperationKind.UPDATE


class EvidenceAppend(_PlanOperation):
    """Append a short evidence block to an existing wiki page."""

    expected_sha256: str = Field(min_length=1)
    content: str
    kind: Literal[OperationKind.APPEND] = OperationKind.APPEND


class MemoryPlan(BaseModel):
    """A structured set of memory operations proposed by MemoryEnricher."""

    model_config = ConfigDict(frozen=True)

    operations: list[_PlanOperation]
    rationale: str
    evidence: list[str]

    def is_noop(self) -> bool:
        return not self.operations

    @model_validator(mode="after")
    def _no_conflicting_operations_on_same_path(self) -> MemoryPlan:
        seen: set[str] = set()
        for op in self.operations:
            if op.path in seen:
                raise ValueError(f"multiple operations target the same path: {op.path}")
            seen.add(op.path)
        return self


class MemoryReceipt(BaseModel):
    """Result of applying (or attempting to apply) a MemoryPlan."""

    model_config = ConfigDict(frozen=True)

    changed_pages: list[PageReference]
    deferred: list[str]
    fallback_used: bool
    fallback_reason: str
    errors: list[str]


class MemoryPlanRecord(BaseModel):
    """A row in `<wiki>/.lies/memory_plans.jsonl` — the JSONL receipt sidecar.

    Mirrors the on-disk JSON schema exactly. Pydantic enforces shape on read;
    `sidecar.append_receipt` constructs rows from a MemoryPlan.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ts: str
    commit_sha: str
    rationale: str
    pages: list[str]
    ops: dict[str, int]
    evidence_count: int
