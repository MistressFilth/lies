from pathlib import PurePosixPath

import pytest
from pydantic import ValidationError

from lies.memory.models import (
    EvidenceAppend,
    IngestQuarantined,
    IngestSourceUnreachable,
    MemoryPlan,
    MemoryReceipt,
    OperationKind,
    PageCreate,
    PageDelete,
    PageReference,
    PageUpdate,
    WikiCollectionRef,
    WikiEvidence,
    WikiMemoryError,
    WikiPlanInvalid,
    WikiSearchResult,
)


def test_collection_ref_is_immutable() -> None:
    ref = WikiCollectionRef(
        collection_id="main",
        root=PurePosixPath("/tmp/wikis/main"),
        qmd_collection="main",
        schema_path=PurePosixPath("/tmp/wikis/main/.lies/schema.md"),
    )
    with pytest.raises(ValidationError):
        ref.collection_id = "other"


def test_evidence_carries_page_id_and_lines() -> None:
    ev = WikiEvidence(
        page_id="page-1",
        path="concepts/example.md",
        collection_id="main",
        excerpt="example excerpt",
        line_start=10,
        line_end=24,
        score=0.81,
    )
    assert ev.line_end - ev.line_start >= 1


def test_search_result_serializes_with_evidence() -> None:
    result = WikiSearchResult(
        query="q",
        pages=[
            WikiEvidence(
                page_id="p",
                path="concepts/x.md",
                collection_id="main",
                excerpt="e",
                line_start=0,
                line_end=5,
                score=0.5,
            )
        ],
        truncated=False,
        fallback_used=False,
        fallback_reason="",
    )
    data = result.model_dump()
    assert len(data["pages"]) == 1


def test_page_create_requires_evidence() -> None:
    with pytest.raises(ValidationError):
        PageCreate(path="concepts/x.md", content="# X", evidence=[])


def test_page_update_requires_expected_hash() -> None:
    with pytest.raises(ValidationError):
        PageUpdate(path="x.md", expected_sha256="", content="x", evidence=["e"])


def test_evidence_append_path_inside_wiki_root() -> None:
    op = EvidenceAppend(
        path="concepts/example.md",
        expected_sha256="abc",
        content="## Note",
        evidence=["page-1"],
    )
    assert op.kind == OperationKind.APPEND


def test_memory_plan_noop_is_valid() -> None:
    plan = MemoryPlan(operations=[], rationale="nothing to file", evidence=[])
    assert plan.is_noop()


def test_memory_plan_rejects_mixed_operations_on_same_path() -> None:
    with pytest.raises(ValidationError):
        MemoryPlan(
            operations=[
                PageCreate(path="x.md", content="a", evidence=["e"]),
                PageUpdate(path="x.md", expected_sha256="h", content="b", evidence=["e"]),
            ],
            rationale="conflicting",
            evidence=["e"],
        )


def test_memory_receipt_carries_change_list() -> None:
    receipt = MemoryReceipt(
        changed_pages=[
            PageReference(path="concepts/example.md", collection_id="main", op=OperationKind.UPDATE)
        ],
        deferred=[],
        fallback_used=False,
        fallback_reason="",
        errors=[],
    )
    assert len(receipt.changed_pages) == 1


def test_typed_error_carries_path_and_message() -> None:
    err = WikiPlanInvalid(path="x.md", reason="missing evidence")
    assert "missing evidence" in str(err)
    assert err.path == "x.md"


def test_ingest_quarantined_is_wiki_memory_error() -> None:
    err = IngestQuarantined(
        source="raw/articles/sample.md",
        collection="claude-code",
        reason="page_writer_agent raised ValidationError: ...",
    )
    assert isinstance(err, WikiMemoryError)
    msg = str(err)
    assert "claude-code" in msg
    assert "raw/articles/sample.md" in msg
    assert "page_writer_agent" in msg


def test_ingest_source_unreachable_is_wiki_memory_error() -> None:
    err = IngestSourceUnreachable(
        source="https://example.com/missing",
        reason="WebScraper.fetch raised ConnectionError",
    )
    assert isinstance(err, WikiMemoryError)
    assert "https://example.com/missing" in str(err)


def test_page_create_default_tag_is_memory() -> None:
    op = PageCreate(path="wiki/foo.md", evidence=["raw/x.md"], content="body")
    assert op.tag == "memory"


def test_page_update_default_tag_is_memory() -> None:
    op = PageUpdate(
        path="wiki/foo.md",
        evidence=["raw/x.md"],
        expected_sha256="abc",
        content="body",
    )
    assert op.tag == "memory"


def test_page_create_with_custom_tag_is_frozen() -> None:
    op = PageCreate(
        path="wiki/foo.md",
        evidence=["raw/x.md"],
        content="body",
        tag="ingest",
    )
    assert op.tag == "ingest"
    with pytest.raises(ValidationError):
        op.tag = "synthesis"  # type: ignore[misc]


def test_memory_plan_rejects_heterogeneous_tags() -> None:
    with pytest.raises(ValidationError):
        MemoryPlan(
            operations=[
                PageCreate(path="x.md", content="a", evidence=["e"], tag="ingest"),
                PageUpdate(
                    path="y.md",
                    expected_sha256="h",
                    content="b",
                    evidence=["e"],
                    tag="memory",
                ),
            ],
            rationale="mixed tags",
            evidence=["e"],
        )


def test_memory_plan_accepts_homogeneous_tags_default() -> None:
    plan = MemoryPlan(
        operations=[
            PageCreate(path="x.md", content="a", evidence=["e"]),
            PageUpdate(
                path="y.md",
                expected_sha256="h",
                content="b",
                evidence=["e"],
            ),
        ],
        rationale="all memory",
        evidence=["e"],
    )
    assert {op.tag for op in plan.operations} == {"memory"}


def test_memory_plan_accepts_homogeneous_tags_custom() -> None:
    plan = MemoryPlan(
        operations=[
            PageCreate(path="x.md", content="a", evidence=["e"], tag="ingest"),
            PageUpdate(
                path="y.md",
                expected_sha256="h",
                content="b",
                evidence=["e"],
                tag="ingest",
            ),
        ],
        rationale="all ingest",
        evidence=["e"],
    )
    assert {op.tag for op in plan.operations} == {"ingest"}


def test_page_delete_carries_evidence_and_kind() -> None:
    op = PageDelete(path="wiki/entities/postgres.md", evidence=["raw/x.md"])
    assert op.kind == OperationKind.DELETE
    assert op.tag == "memory"
    assert op.path == "wiki/entities/postgres.md"
    assert op.evidence == ["raw/x.md"]


def test_page_delete_requires_evidence() -> None:
    with pytest.raises(ValidationError):
        PageDelete(path="wiki/foo.md", evidence=[])
