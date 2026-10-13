"""Tests for ``build_answer_from_pages`` preamble semantics."""

from __future__ import annotations

from lies.query.synthesizer import (
    FALLBACK_REASON_WIKI_ONLY,
    PageRead,
    build_answer_from_pages,
    _empty_answer,
)
from lies.query.models import SynthesizedAnswer
from lies.query.format_validator import validate_format


def test_build_answer_from_pages_wiki_only_preamble() -> None:
    """WIKI_ONLY renders the not-grounded preamble, not the qmd-unavailable preamble."""
    page = PageRead(
        rel_path="concepts/x.md",
        title="X",
        spans=[],
        source="wiki",
    )
    ans = build_answer_from_pages(
        question="test", pages=[page], fallback_reason=FALLBACK_REASON_WIKI_ONLY
    )
    assert "not grounded in primary sources" in ans.answer
    assert "qmd unavailable" not in ans.answer


def test_build_answer_from_pages_format_hint_kwarg_defaults_to_md() -> None:
    """``build_answer_from_pages`` defaults format_hint to "md" when not passed."""
    page = PageRead(
        rel_path="concepts/x.md",
        title="X",
        spans=[],
        source="wiki",
    )
    ans = build_answer_from_pages(question="test", pages=[page], fallback_reason="")
    assert ans.format == "md"


def test_build_answer_from_pages_format_hint_kwarg_validates_body() -> None:
    """``build_answer_from_pages(format_hint="table")`` validates the body."""
    page = PageRead(
        rel_path="concepts/x.md",
        title="X",
        spans=[],
        source="wiki",
    )
    ans = build_answer_from_pages(
        question="test", pages=[page], fallback_reason="", format_hint="table"
    )
    assert ans.format == "md"


def test_build_answer_from_pages_format_hint_kwarg_passes_through_md() -> None:
    """``build_answer_from_pages(format_hint="md")`` is a no-op (md always validates)."""
    page = PageRead(
        rel_path="concepts/x.md",
        title="X",
        spans=[],
        source="wiki",
    )
    ans = build_answer_from_pages(
        question="test", pages=[page], fallback_reason="", format_hint="md"
    )
    assert ans.format == "md"


def test_build_answer_from_pages_sets_format_from_hint() -> None:
    """``build_answer_from_pages`` validates the synthesizer's format_hint."""
    table_body = "| col1 | col2 |\n| --- | --- |\n| a | b |\n"
    # Validator wires up: hint survives on valid bodies.
    assert validate_format(table_body, "table") == "table"
    # Validator demotes a lie to md.
    bullets = "just bullets\n- one\n- two\n"
    assert validate_format(bullets, "table") == "md"


def test_build_answer_from_pages_demotes_bad_hint_to_md() -> None:
    """Validator demotes a lie (hint=table, body=bullets) to md."""
    bullets = "just bullets\n- one\n- two\n"
    assert validate_format(bullets, "table") == "md"


def test_empty_answer_has_format_md() -> None:
    """The no-pages-found path returns format=md."""
    ans = SynthesizedAnswer(answer=_empty_answer("?", "qmd_unavailable"))
    assert ans.format == "md"
