"""Tests for the SynthesizedAnswer citations/pages_read shape (Task 6)."""

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
    """The annotations must be ``list[Citation]``, not ``list[str]``."""
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
    """``pages_read`` entries thread ``line`` and ``section`` per spec."""
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
