"""Tests for the SynthesizedAnswer citations/pages_read shape (Task 6).

The Bundle C refactor hard-cuts the shape from ``list[str]`` to
``list[Citation]`` so the source discriminator rides with each
citation. These tests pin the new shape.

The runtime tests pass against the pre-change implementation too
(Python does not enforce dataclass field types at runtime), so we
additionally assert the field annotation explicitly via
:func:`typing.get_type_hints` to make the cutover observable without
the type checker in the loop.
"""

from __future__ import annotations

import typing

from lies.query.citation import Citation, ClaimCitation
from lies.query.models import SynthesizedAnswer


def test_synthesized_answer_citations_is_list_of_citation() -> None:
    ans = SynthesizedAnswer(
        answer="x",
        citations=[Citation(path="a.md", source="library")],
        pages_read=[Citation(path="a.md", source="library")],
    )
    assert isinstance(ans.citations[0], Citation)
    assert ans.citations[0].source == "library"


def test_synthesized_answer_empty_lists_default() -> None:
    ans = SynthesizedAnswer(answer="x")
    assert ans.citations == []
    assert ans.pages_read == []


def test_synthesized_answer_annotations_are_list_of_citation() -> None:
    """The annotations must be ``list[Citation]``, not ``list[str]``.

    Runtime field assignment accepts any object regardless of the
    annotation, so a runtime-only test would pass against both the
    pre- and post-change implementations. Inspecting the annotation
    surfaces the cutover: ``list[Citation]`` requires ``Citation``,
    ``list[str]`` does not.
    """
    hints = typing.get_type_hints(SynthesizedAnswer)
    assert hints["citations"] == list[Citation]
    assert hints["pages_read"] == list[Citation]


def test_synthesized_answer_claim_citations_default_empty() -> None:
    ans = SynthesizedAnswer(answer="x")
    assert ans.claim_citations == ()


def test_synthesized_answer_accepts_claim_citations() -> None:
    cc = ClaimCitation(claim="foo", citation_index=0)
    ans = SynthesizedAnswer(
        answer="foo bar",
        citations=[Citation(path="x.md", source="wiki")],
        claim_citations=[cc],
    )
    assert ans.claim_citations == (cc,)


def test_synthesized_answer_claim_citations_annotation() -> None:
    hints = typing.get_type_hints(SynthesizedAnswer)
    assert hints["claim_citations"] == tuple[ClaimCitation, ...]


def test_pages_read_carries_line_and_section() -> None:
    """``pages_read`` entries thread ``line`` and ``section`` per spec.

    Both ``run_query`` and ``run_query_with_format`` must populate
    ``line`` and ``section`` from the underlying ``PageRead`` so
    downstream consumers (footnote renderer, MCP response envelope)
    can render per-passage anchors without re-querying.
    """
    cc = Citation(
        path="a.md",
        source="library",
        line=42,
        section="Context isolation",
    )
    ans = SynthesizedAnswer(
        answer="x",
        pages_read=[cc],
    )
    assert ans.pages_read == [cc]
    assert ans.pages_read[0].line == 42
    assert ans.pages_read[0].section == "Context isolation"
