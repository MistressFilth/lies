"""Tests for build_author_plan non-synthesis branch + _format_author_body.

F3's synthesis branch is migrated in Task 5; F17's required-section
enforcement is out out scope.
"""

from __future__ import annotations

import pytest

from lies.memory.models import (
    MemoryPlan,
    PageCreate,
    PageUpdate,
    WikiPlanInvalid,
)
from lies.page import build_author_plan, _format_author_body


# ---------- helpers ----------


def _exists_always_false(_rel: str) -> bool:
    return False


def _sha_lookup(_rel: str) -> str:
    return "deadbeef" * 8  # 64 hex chars


# ---------- type validation ----------


def test_invalid_type_raises():
    with pytest.raises(WikiPlanInvalid, match="not in ALLOWED_PAGE_TYPES"):
        build_author_plan(
            type="bogus",  # type: ignore[arg-type]
            collection="c",
            slug="s",
            title="T",
            body="b",
            derived_from=[],
            tags=[],
            sources=[],
            exists=_exists_always_false,
        )


# ---------- PageCreate path (no collision) ----------


@pytest.mark.parametrize(
    "page_type,type_plural",
    [
        ("entity", "entities"),
        ("concept", "concepts"),
        ("comparison", "comparisons"),
        ("source", "sources"),
    ],
)
def test_page_create_for_new_path(page_type, type_plural):
    plan = build_author_plan(
        type=page_type,
        collection="claude-code",
        slug="hooks",
        title="Hooks",
        body="## Body\nContent.",
        derived_from=[],
        tags=["t1"],
        sources=[],
        exists=_exists_always_false,
    )
    assert isinstance(plan, MemoryPlan)
    assert len(plan.operations) == 1
    op = plan.operations[0]
    assert isinstance(op, PageCreate)
    assert op.path == f"claude-code/{type_plural}/hooks.md"
    assert op.tag == "author"
    assert op.evidence == ["claude-code/hooks"]


def test_page_create_evidence_is_collection_slash_slug():
    plan = build_author_plan(
        type="entity",
        collection="x",
        slug="y",
        title="T",
        body="b",
        derived_from=[],
        tags=[],
        sources=[],
        exists=_exists_always_false,
    )
    assert plan.operations[0].evidence == ["x/y"]


def test_empty_body_raises():
    with pytest.raises(WikiPlanInvalid, match="body"):
        build_author_plan(
            type="concept",
            collection="c",
            slug="s",
            title="T",
            body="   \n  ",
            derived_from=[],
            tags=[],
            sources=[],
            exists=_exists_always_false,
        )


# ---------- PageUpdate path (collision) ----------


def test_page_update_when_path_exists():
    def exists(rel: str) -> bool:
        return rel == "claude-code/concepts/hooks.md"

    plan = build_author_plan(
        type="concept",
        collection="claude-code",
        slug="hooks",
        title="Hooks",
        body="Updated body.",
        derived_from=[],
        tags=[],
        sources=[],
        exists=exists,
        sha_lookup=_sha_lookup,
    )
    op = plan.operations[0]
    assert isinstance(op, PageUpdate)
    assert op.path == "claude-code/concepts/hooks.md"
    assert op.expected_sha256 == "deadbeef" * 8
    assert op.tag == "author"


def test_page_update_without_sha_lookup_raises():
    def exists(rel: str) -> bool:
        return True

    with pytest.raises(WikiPlanInvalid, match="sha_lookup"):
        build_author_plan(
            type="concept",
            collection="c",
            slug="s",
            title="T",
            body="b",
            derived_from=[],
            tags=[],
            sources=[],
            exists=exists,
            sha_lookup=None,
        )


# ---------- _format_author_body ----------


def test_format_body_quotes_title_and_collection():
    out = _format_author_body(
        type="concept",
        collection="claude-code",
        title='Has "quotes" inside',
        body="body content",
        derived_from=[],
        tags=["a", "b"],
        sources=[],
    )
    # YAML safety: both fields are double-quoted
    assert '"Has \\"quotes\\" inside"' in out or '"Has "quotes" inside"' in out
    assert 'collection: "claude-code"' in out
    assert "tags: [a, b]" in out or "tags:\n  - a\n  - b" in out


def test_format_body_includes_type():
    out = _format_author_body(
        type="entity",
        collection="c",
        title="T",
        body="body",
        derived_from=[],
        tags=[],
        sources=[],
    )
    assert "type: entity" in out


def test_format_body_includes_sources():
    out = _format_author_body(
        type="source",
        collection="c",
        title="T",
        body="body",
        derived_from=[],
        tags=[],
        sources=["raw/articles/x.md"],
    )
    assert "raw/articles/x.md" in out


def test_format_body_includes_derived_from():
    out = _format_author_body(
        type="concept",
        collection="c",
        title="T",
        body="body",
        derived_from=["claude-code/concepts/hooks"],
        tags=[],
        sources=[],
    )
    assert "claude-code/concepts/hooks" in out


def test_format_body_emits_frontmatter_then_body():
    out = _format_author_body(
        type="concept",
        collection="c",
        title="T",
        body="## Section\nContent",
        derived_from=[],
        tags=[],
        sources=[],
    )
    assert out.startswith("---\n")
    # body appears after frontmatter
    fm_end = out.index("\n---\n", 4)
    body = out[fm_end + 5 :]
    assert "## Section" in body
    assert "Content" in body
