"""Scaffold test for the new lies.page package."""

from __future__ import annotations

from typing import Literal

import pytest
from pydantic import BaseModel

from lies.page import WriteKnowledgeResult, build_author_plan


def test_write_knowledge_result_is_pydantic_basemodel():
    """WriteKnowledgeResult is a frozen Pydantic BaseModel (slice shape)."""
    assert isinstance(WriteKnowledgeResult, type)
    assert issubclass(WriteKnowledgeResult, BaseModel)
    # ConfigDict(frozen=True) → instances are immutable
    r = WriteKnowledgeResult(
        page_path="claude-code/concepts/hooks.md",
        page_type="concept",
        slug="hooks",
        collection="claude-code",
        op="create",
        receipt={
            "changed_pages": [],
            "deferred": [],
            "fallback_used": False,
            "fallback_reason": "",
            "errors": [],
        },
    )
    with pytest.raises(Exception):
        r.page_path = "other.md"  # type: ignore[misc]


def test_write_knowledge_result_op_literal():
    """op is Literal['create', 'update', 'none']."""
    fields = WriteKnowledgeResult.model_fields
    assert fields["op"].annotation == Literal["create", "update", "none"]


def test_build_author_plan_is_callable():
    """build_author_plan exists and is callable (skeleton implementation OK)."""
    assert callable(build_author_plan)


def test_build_author_plan_synthesis_returns_plan_with_tag():
    """The synthesis branch returns a plan tagged ``synthesis``; an empty ``derived_from`` still raises."""
    from lies.memory.models import WikiPlanInvalid

    # Non-empty derived_from → success with synthesis tag.
    plan = build_author_plan(
        type="synthesis",
        collection="claude-code",
        slug="hooks",
        title="Hooks",
        body="body",
        derived_from=["claude-code/concepts/hooks"],
        tags=["synthesis"],
        sources=[],
        exists=lambda r: False,
    )
    assert plan.operations[0].tag == "synthesis"

    # Empty derived_from → still raises WikiPlanInvalid.
    with pytest.raises(WikiPlanInvalid, match="derived_from"):
        build_author_plan(
            type="synthesis",
            collection="claude-code",
            slug="hooks",
            title="Hooks",
            body="body",
            derived_from=[],
            tags=[],
            sources=[],
            exists=lambda r: False,
        )
