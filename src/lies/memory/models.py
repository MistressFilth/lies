"""Pydantic models for wiki memory.

The models are the public contract used by the Pydantic AI read tools,
the MemoryEnricher structured output, the WikiMemoryService, and the
FastMCP adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

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


class IngestQuarantined(WikiMemoryError):
    """A single-source ingest failed at the agent layer.

    The source has been copied to
    ``$XDG_STATE_HOME/lies/<wiki>/poison/<collection>/<relpath>``
    with a ``.reason`` sidecar (see :mod:`lies.etl.quarantine`).
    """

    def __init__(self, source: str, collection: str, reason: str) -> None:
        super().__init__(
            f"ingest-source {source!r} for collection {collection!r} quarantined: {reason}"
        )
        self.source = source
        self.collection = collection
        self.reason = reason


class IngestSourceUnreachable(WikiMemoryError):
    """A single-source ingest could not materialize the source to disk.

    Raised before any wiki write or quarantine copy; the source
    artifact is not on disk yet (URL fetch failed, stdin closed, or
    the local path was missing).
    """

    def __init__(self, source: str, reason: str) -> None:
        super().__init__(f"ingest-source unreachable: {source!r}: {reason}")
        self.source = source
        self.reason = reason


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
    DELETE = "delete"


@dataclass(frozen=True, kw_only=True)
class _PlanOperation:
    """Base for plan operations. Subclasses redeclare ``kind`` with a
    concrete ``Literal[OperationKind.X]`` value (Pydantic's union
    discriminator becomes a class type).
    """

    path: str
    evidence: list[str]
    kind: OperationKind
    tag: str = "memory"

    def __post_init__(self) -> None:
        if len(self.evidence) < 1:
            raise ValueError(
                f"plan op {self.kind!r} on {self.path!r}: at least one evidence reference required"
            )


@dataclass(frozen=True, kw_only=True)
class PageCreate(_PlanOperation):
    """Create a new wiki page."""

    content: str
    kind: Literal[OperationKind.CREATE] = OperationKind.CREATE
    tag: str = "memory"


@dataclass(frozen=True, kw_only=True)
class PageUpdate(_PlanOperation):
    """Replace a wiki page with a versioned update."""

    content: str
    expected_sha256: str
    kind: Literal[OperationKind.UPDATE] = OperationKind.UPDATE
    tag: str = "memory"

    def __post_init__(self) -> None:
        super().__post_init__()
        if len(self.expected_sha256) < 1:
            raise ValueError(f"PageUpdate on {self.path!r}: expected_sha256 must be non-empty")


@dataclass(frozen=True, kw_only=True)
class EvidenceAppend(_PlanOperation):
    """Append a short evidence block to an existing wiki page."""

    content: str
    expected_sha256: str
    kind: Literal[OperationKind.APPEND] = OperationKind.APPEND
    tag: str = "memory"

    def __post_init__(self) -> None:
        super().__post_init__()
        if len(self.expected_sha256) < 1:
            raise ValueError(f"EvidenceAppend on {self.path!r}: expected_sha256 must be non-empty")


@dataclass(frozen=True, kw_only=True)
class PageDelete(_PlanOperation):
    """Remove an existing wiki page. No-op if the page does not exist."""

    kind: Literal[OperationKind.DELETE] = OperationKind.DELETE
    tag: str = "memory"


@dataclass(frozen=True)
class MemoryPlan:
    """A structured set of memory operations proposed by MemoryEnricher."""

    operations: list[_PlanOperation]
    rationale: str
    evidence: list[str]

    def __post_init__(self) -> None:
        seen: set[str] = set()
        for op in self.operations:
            if op.path in seen:
                raise ValueError(f"multiple operations target the same path: {op.path}")
            seen.add(op.path)
        tags = {op.tag for op in self.operations}
        if len(tags) > 1:
            raise ValueError(f"MemoryPlan ops must share one tag; got {sorted(tags)!r}")

    def is_noop(self) -> bool:
        return not self.operations


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
