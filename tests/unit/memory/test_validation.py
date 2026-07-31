# tests/unit/memory/test_validation.py
from pathlib import Path
from textwrap import dedent

import pytest

from lies.memory.models import (
    EvidenceAppend,
    OperationKind,
    PageCreate,
    PageUpdate,
    WikiEvidenceMissing,
    WikiPlanInvalid,
)
from lies.memory.validation import (
    parse_frontmatter,
    validate_frontmatter,
    validate_operation_evidence,
    validate_page_path,
    validate_page_type,
)
from lies.wiki.layout import WikiLayout


@pytest.fixture
def layout(tmp_path: Path) -> WikiLayout:
    root = tmp_path / "wiki"
    (root / "wiki").mkdir(parents=True)
    (root / ".lies").mkdir(parents=True)
    (root / "raw").mkdir(parents=True)
    return WikiLayout(root)


def test_validate_page_path_rejects_traversal(layout: WikiLayout) -> None:
    with pytest.raises(WikiPlanInvalid):
        validate_page_path(layout, "../outside.md")


def test_validate_page_path_rejects_raw(layout: WikiLayout) -> None:
    with pytest.raises(WikiPlanInvalid):
        validate_page_path(layout, "../raw/article.md")


def test_validate_page_path_accepts_concepts(layout: WikiLayout) -> None:
    resolved = validate_page_path(layout, "concepts/x.md")
    assert resolved == layout.wiki_dir / "concepts" / "x.md"


def test_validate_page_type_rejects_unknown(layout: WikiLayout) -> None:
    with pytest.raises(WikiPlanInvalid):
        validate_page_type("rumor")


def test_parse_frontmatter_empty() -> None:
    assert parse_frontmatter("# hi") == {}


def test_parse_frontmatter_full() -> None:
    text = dedent(
        """\
        ---
        title: Hi
        type: concept
        created: 2026-07-29
        ---
        # Hi
        """
    )
    fm = parse_frontmatter(text)
    assert fm["title"] == "Hi"
    assert fm["type"] == "concept"


def test_validate_frontmatter_missing_type() -> None:
    with pytest.raises(WikiPlanInvalid):
        validate_frontmatter({"title": "x"}, page_type="concept")


def test_validate_operation_evidence_present() -> None:
    op = PageCreate(path="x.md", content="# X", evidence=["page-1"])
    validate_operation_evidence(op)  # no raise


def test_validate_operation_evidence_missing() -> None:
    op = PageCreate.model_construct(
        path="x.md", content="# X", evidence=[], kind=OperationKind.CREATE
    )
    with pytest.raises(WikiEvidenceMissing):
        validate_operation_evidence(op)


def test_validate_operation_evidence_update_requires_hash() -> None:
    op = PageUpdate.model_construct(
        path="x.md", expected_sha256="", content="x", evidence=["e"], kind=OperationKind.UPDATE
    )
    with pytest.raises(WikiPlanInvalid):
        validate_operation_evidence(op)


def test_validate_operation_evidence_append_requires_hash() -> None:
    op = EvidenceAppend.model_construct(
        path="x.md", expected_sha256="", content="x", evidence=["e"], kind=OperationKind.APPEND
    )
    with pytest.raises(WikiPlanInvalid):
        validate_operation_evidence(op)


def test_validate_operation_evidence_rejects_unknown_reference() -> None:
    op = PageCreate(path="concepts/x.md", content="# X", evidence=["invented"])
    with pytest.raises(WikiEvidenceMissing, match="unknown evidence"):
        validate_operation_evidence(op, known_references={"page-1", "wiki/source.md:2-4"})


def test_validate_operation_evidence_accepts_page_path_and_line_range() -> None:
    op = PageCreate(
        path="concepts/x.md",
        content="# X",
        evidence=["wiki/source.md", "wiki/source.md:2-4"],
    )
    validate_operation_evidence(
        op,
        known_references={"wiki/source.md", "wiki/source.md:2-4"},
    )
