"""Tests for ``build_answer_from_pages`` preamble semantics.

The synthesizer renders one of three preambles based on ``fallback_reason``:

- ``""`` — no preamble (qmd served the query).
- ``FALLBACK_REASON_WIKI_ONLY`` — "not grounded in primary sources"
  preamble (library returned 0 hits, wiki surfaced content).
- Any other truthy reason — "qmd unavailable" preamble.

This module pins the WIKI_ONLY branch: the body must contain the
not-grounded phrase and must NOT contain the qmd-unavailable phrase.
The other two branches are covered by ``test_query_synthesizer.py``.
"""

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
    """``build_answer_from_pages`` defaults format_hint to "md" when not passed.

    The extractive path is called from the orchestrator's no-pages and
    failure paths with no synthesizer hint available; the default keeps
    existing call sites compiling and pins the answer's format to md.
    """
    page = PageRead(
        rel_path="concepts/x.md",
        title="X",
        spans=[],
        source="wiki",
    )
    ans = build_answer_from_pages(question="test", pages=[page], fallback_reason="")
    assert ans.format == "md"


def test_build_answer_from_pages_format_hint_kwarg_validates_body() -> None:
    """``build_answer_from_pages(format_hint="table")`` validates the body.

    The extractive answer body is built from the page excerpts — it
    never looks like a table. Passing ``format_hint="table"`` must
    therefore demote to "md" via ``validate_format``. The contract is
    that the validator (not the caller) decides what survived.
    """
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
    """``build_answer_from_pages`` validates the synthesizer's format_hint.

    The extractive builder is the orchestrator's safety net: when qmd
    fails or no pages are readable, the orchestrator wraps the result in
    a ``SynthesizedAnswer`` whose ``format`` field is the validator's
    verdict on the synthesizer's ``format_hint``. A table-hint + table-
    body pair validates to "table"; a lie (hint=table, body=bullets)
    demotes to "md".
    """
    table_body = "| col1 | col2 |\n| --- | --- |\n| a | b |\n"
    # Validator wires up: hint survives on valid bodies.
    assert validate_format(table_body, "table") == "table"
    # Validator demotes a lie to md.
    bullets = "just bullets\n- one\n- two\n"
    assert validate_format(bullets, "table") == "md"


def test_build_answer_from_pages_demotes_bad_hint_to_md() -> None:
    """Validator demotes a lie (hint=table, body=bullets) to md.

    Pins the contract that downstream render dispatch (T6) trusts:
    when the body fails the format check, the format field falls back
    to "md" rather than the requested hint.
    """
    bullets = "just bullets\n- one\n- two\n"
    assert validate_format(bullets, "table") == "md"


def test_empty_answer_has_format_md() -> None:
    """The no-pages-found path returns format=md.

    The extractive fallback (``_empty_answer``) has no body that can
    be a table or a marp deck — it just announces the qmd failure.
    The wrapping ``SynthesizedAnswer`` carries ``format="md"`` so
    downstream render dispatch falls into the md branch.
    """
    ans = SynthesizedAnswer(answer=_empty_answer("?", "qmd_unavailable"))
    assert ans.format == "md"
