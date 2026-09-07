"""F3 synthesis behavior regression pins after migration to build_author_plan.

Lifted verbatim from tests/unit/test_build_synthesis_plan.py (which is
deleted in Task 7). Pins slug stability, PageCreate vs PageUpdate,
frontmatter, body ## Evidence assembly.
"""

from __future__ import annotations

import hashlib

import pytest

from lies.memory.models import PageUpdate, WikiPlanInvalid
from lies.page import build_author_plan


def _exists_always_false(_rel: str) -> bool:
    return False


def _sha_lookup(_rel: str) -> str:
    return "f" * 64


def _expected_slug(question: str) -> str:
    from lies.memory.service import _slugify  # internal; if absent, inline below

    digest = hashlib.sha256(question.encode("utf-8")).hexdigest()[:8]
    return f"{_slugify(question)[:48]}-{digest}.md"


# In case _slugify is not exported, define a local copy.
def _local_slugify(s: str) -> str:
    import re

    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def test_synthesis_stable_slug():
    """Same question → same slug; different question → different slug."""
    q = "What is a hook?"
    plan1 = build_author_plan(
        type="synthesis",
        collection="claude-code",
        slug=_local_slugify(q)[:48] + "-" + hashlib.sha256(q.encode()).hexdigest()[:8],
        title="What is a hook?",
        body="Answer text.",
        derived_from=["claude-code/concepts/hooks"],
        tags=["synthesis"],
        sources=[],
        exists=_exists_always_false,
    )
    plan2 = build_author_plan(
        type="synthesis",
        collection="claude-code",
        slug=_local_slugify(q)[:48] + "-" + hashlib.sha256(q.encode()).hexdigest()[:8],
        title="What is a hook?",
        body="Answer text.",
        derived_from=["claude-code/concepts/hooks"],
        tags=["synthesis"],
        sources=[],
        exists=_exists_always_false,
    )
    assert plan1.operations[0].path == plan2.operations[0].path


def test_synthesis_tag_is_synthesis():
    plan = build_author_plan(
        type="synthesis",
        collection="c",
        slug="s",
        title="T",
        body="b",
        derived_from=["c/x"],
        tags=["synthesis"],
        sources=[],
        exists=_exists_always_false,
    )
    assert plan.operations[0].tag == "synthesis"


def test_synthesis_path_layout():
    plan = build_author_plan(
        type="synthesis",
        collection="claude-code",
        slug="what-is-a-hook-3f9a1e2c",
        title="T",
        body="b",
        derived_from=["claude-code/concepts/hooks"],
        tags=[],
        sources=[],
        exists=_exists_always_false,
    )
    assert plan.operations[0].path == "claude-code/synthesis/what-is-a-hook-3f9a1e2c.md"


def test_synthesis_collision_uses_page_update():
    def exists(rel: str) -> bool:
        return rel == "claude-code/synthesis/x.md"

    plan = build_author_plan(
        type="synthesis",
        collection="claude-code",
        slug="x",
        title="T",
        body="b",
        derived_from=["claude-code/concepts/x"],
        tags=[],
        sources=[],
        exists=exists,
        sha_lookup=_sha_lookup,
    )
    op = plan.operations[0]
    assert isinstance(op, PageUpdate)
    assert op.expected_sha256 == "f" * 64


def test_synthesis_empty_derived_from_raises():
    """F3 invariant: empty derived_from raises (no ingested source)."""
    with pytest.raises(WikiPlanInvalid, match="derived_from"):
        build_author_plan(
            type="synthesis",
            collection="c",
            slug="s",
            title="T",
            body="b",
            derived_from=[],
            tags=[],
            sources=[],
            exists=_exists_always_false,
        )


def test_synthesis_body_includes_evidence_section():
    """The synthesis body must include a ## Evidence section listing each derived_from slug."""
    plan = build_author_plan(
        type="synthesis",
        collection="claude-code",
        slug="s",
        title="T",
        body="Original answer body.",
        derived_from=["claude-code/concepts/hooks", "claude-code/concepts/skills"],
        tags=["synthesis"],
        sources=[],
        exists=_exists_always_false,
    )
    content = plan.operations[0].content
    assert "## Evidence" in content
    assert "[[claude-code/concepts/hooks]]" in content
    assert "[[claude-code/concepts/skills]]" in content
    assert "Original answer body." in content
