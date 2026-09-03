"""Tests for build_synthesis_plan."""

from __future__ import annotations

import hashlib

import pytest

from lies.memory.models import (
    MemoryPlan,
    OperationKind,
    PageCreate,
    PageUpdate,
    WikiPlanInvalid,
)
from lies.memory.service import (
    _format_synthesis_body,
    _page_type_from_dir,
    build_synthesis_plan,
)


QUESTION = "what is a hook?"
ANSWER = "A hook intercepts events at fixed points in the agent's lifecycle."
PAGES_READ = [
    "claude-code/concepts/hooks",
    "claude-code/concepts/skills",
]
COLLECTION = "claude-code"


def _expected_slug() -> str:
    safe = "what-is-a-hook"
    digest = hashlib.sha256(QUESTION.encode("utf-8")).hexdigest()[:8]
    return f"{safe}-{digest}.md"


def test_build_synthesis_plan_returns_page_create_for_new_slug() -> None:
    plan = build_synthesis_plan(
        question=QUESTION,
        answer=ANSWER,
        pages_read=PAGES_READ,
        collection=COLLECTION,
    )
    assert isinstance(plan, MemoryPlan)
    assert len(plan.operations) == 1
    op = plan.operations[0]
    assert isinstance(op, PageCreate)
    assert op.path == f"claude-code/synthesis/{_expected_slug()}"
    assert op.kind == OperationKind.CREATE
    assert op.tag == "synthesis"
    assert op.evidence == PAGES_READ
    assert "## Evidence" in op.content
    assert "[[claude-code/concepts/hooks]]" in op.content
    assert "[[claude-code/concepts/skills]]" in op.content
    assert "derived_from:" in op.content
    assert "tags: [synthesis]" in op.content


def test_build_synthesis_plan_returns_page_update_on_collision() -> None:
    sha = "deadbeef" * 8  # 64 hex chars
    expected_path = f"claude-code/synthesis/{_expected_slug()}"
    existing_paths = {expected_path}

    def fake_exists(rel: str) -> bool:
        return rel in existing_paths

    plan = build_synthesis_plan(
        question=QUESTION,
        answer=ANSWER,
        pages_read=PAGES_READ,
        collection=COLLECTION,
        exists=fake_exists,
        sha_lookup=lambda rel: sha if rel == expected_path else "",
    )
    op = plan.operations[0]
    assert isinstance(op, PageUpdate)
    assert op.expected_sha256 == sha
    assert op.path == expected_path


def test_build_synthesis_plan_slug_is_stable() -> None:
    plan_a = build_synthesis_plan(
        question=QUESTION,
        answer=ANSWER,
        pages_read=PAGES_READ,
        collection=COLLECTION,
    )
    plan_b = build_synthesis_plan(
        question=QUESTION,
        answer=ANSWER,
        pages_read=PAGES_READ,
        collection=COLLECTION,
    )
    assert plan_a.operations[0].path == plan_b.operations[0].path


def test_build_synthesis_plan_empty_pages_read_raises() -> None:
    with pytest.raises(WikiPlanInvalid):
        build_synthesis_plan(
            question=QUESTION,
            answer=ANSWER,
            pages_read=[],
            collection=COLLECTION,
        )


def test_build_synthesis_plan_collision_without_sha_lookup_raises() -> None:
    """PageUpdate needs sha_lookup; a collision without one raises WikiPlanInvalid."""
    existing_paths = {f"claude-code/synthesis/{_expected_slug()}"}

    def fake_exists(rel: str) -> bool:
        return rel in existing_paths

    with pytest.raises(WikiPlanInvalid):
        build_synthesis_plan(
            question=QUESTION,
            answer=ANSWER,
            pages_read=PAGES_READ,
            collection=COLLECTION,
            exists=fake_exists,
            # sha_lookup omitted on purpose; the helper detects the
            # collision via an `exists` probe and requires sha_lookup.
        )


def test_page_type_from_dir_preserves_synthesis() -> None:
    """Bug 1 regression: ``synthesis`` is its own singular form.

    The naive ``removesuffix("s")`` branch used to mangle
    ``"synthesis"`` into ``"synthesi"``, which then failed
    ``validate_page_type`` and made the synthesis file-back path
    impossible to validate.
    """
    assert _page_type_from_dir("synthesis") == "synthesis"
    # Spot-check the other branches still work after the special-case.
    assert _page_type_from_dir("concepts") == "concept"
    assert _page_type_from_dir("entities") == "entity"
    assert _page_type_from_dir("comparisons") == "comparison"


def test_synthesis_body_declares_type_in_frontmatter() -> None:
    """Bug 2 regression: ``type: synthesis`` must be in the frontmatter.

    ``validate_frontmatter`` rejects a body whose declared ``type:``
    is missing or mismatched against the derived ``page_type``. Without
    this line, ``validate_plan`` raised ``WikiPlanInvalid`` and the
    file-back receipt returned ``plan_invalid: ...``.
    """
    body = _format_synthesis_body(
        question=QUESTION,
        answer=ANSWER,
        pages_read=PAGES_READ,
        collection=COLLECTION,
    )
    # Frontmatter sits between the first pair of ``---`` fences.
    head = body.split("---", 2)[1]
    assert "type: synthesis" in head
    # Cross-check via the end-to-end plan builder so the line survives
    # any future refactor of ``_format_synthesis_body``.
    plan = build_synthesis_plan(
        question=QUESTION,
        answer=ANSWER,
        pages_read=PAGES_READ,
        collection=COLLECTION,
    )
    assert "type: synthesis" in plan.operations[0].content
