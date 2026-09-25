"""Tests for src/lies/mcp/grounding.py — Task 1 fan-out wiring.

Pins the unscoped-query brick-wall fix: ``ArchivistDigest.no_library``
additive field, ``_fanout_unscoped()`` parallel dispatcher, and the
``no_library=True`` fast-path when no library collections are
registered. The fan-out path replaces the F18 librarian LLM round-trip
on unscoped queries (which timed out at ~42s / returned 0 citations
per session 2505630b's reproduction) with direct qmd fan-out across
registered library collections.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field

import pytest


@pytest.fixture(autouse=True)
def _silence_wiring_skipped_warning() -> None:
    """Silence the ``ground: tool wiring skipped`` warning by default."""
    warnings.filterwarnings(
        "ignore",
        message=r"^ground: tool wiring skipped\b",
        category=UserWarning,
    )


def test_archivist_digest_has_no_library_field_default_false() -> None:
    """`no_library` defaults to False for back-compat with existing call sites."""
    from lies.mcp.grounding import ArchivistDigest

    digest = ArchivistDigest(
        question="q",
        tag_expr=None,
        exclude_expr=None,
        citations=[],
        no_coverage=True,
        distinct_pages=0,
        searched_scope=[],
    )
    assert digest.no_library is False


def test_ground_unscoped_uses_fanout(monkeypatch) -> None:
    """Unscoped ground() calls _fanout_unscoped and merges results."""
    from lies.markdown_spans import Span
    from lies.mcp import grounding
    from lies.query import synthesizer as synth_mod

    # Make the registry appear populated so the ``no_library=True``
    # fast-path is bypassed and the fan-out branch fires.
    monkeypatch.setattr(synth_mod, "_all_collection_names", lambda: ["switchyard"])

    @dataclass(frozen=True)
    class _FakeExcerpt:
        collection: str
        slug: str
        title: str
        spans: list = field(default_factory=list)
        source_kind: str = "library"

    fake_excerpts = [
        _FakeExcerpt(
            collection="switchyard",
            slug="switchyard/install.md",
            title="Install",
            spans=[
                Span(
                    heading_path=[],
                    body="install Switchyard binary",
                    code_fence=False,
                    start_line=1,
                )
            ],
        ),
    ]

    async def fake_fanout(question, exclude_expr, top_k):
        assert question == "test question"
        assert exclude_expr is None
        assert top_k == 5
        return fake_excerpts

    monkeypatch.setattr(grounding, "_fanout_unscoped", fake_fanout)

    digest = grounding.ground("test question", tag_expr=None, top_k=5)
    assert digest.no_coverage is False
    assert digest.no_library is False
    assert len(digest.citations) == 1
    assert digest.citations[0].slug == "switchyard/install.md"


def test_ground_unscoped_no_library_returns_no_library_true(monkeypatch) -> None:
    """When no library collections are registered, ground() returns
    no_library=True and no_coverage=True with empty citations."""
    from lies.mcp import grounding
    from lies.query import synthesizer as synth_mod

    monkeypatch.setattr(synth_mod, "_all_collection_names", lambda: [])

    digest = grounding.ground("any question", tag_expr=None)
    assert digest.no_library is True
    assert digest.no_coverage is True
    assert digest.citations == []
    assert digest.searched_scope == []
