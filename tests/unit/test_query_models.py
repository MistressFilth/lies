"""Unit tests for the SynthesizedAnswer synthesis-provenance fields."""

from __future__ import annotations

from lies.memory.models import MemoryReceipt
from lies.query.models import SynthesizedAnswer


def test_synthesis_fields_default_to_absent() -> None:
    """Existing construction sites must keep working unchanged."""
    answer = SynthesizedAnswer(answer="body")
    assert answer.synthesis_used is False
    assert answer.synthesis_reason == ""
    assert answer.should_file is False


def test_synthesis_fields_are_settable() -> None:
    answer = SynthesizedAnswer(
        answer="body",
        synthesis_used=True,
        synthesis_reason="dropped 1 unretrieved citation(s): ghost.md",
        should_file=True,
    )
    assert answer.synthesis_used is True
    assert answer.synthesis_reason.startswith("dropped 1")
    assert answer.should_file is True


def test_retrieval_axis_is_independent_of_synthesis_axis() -> None:
    """qmd can fail while synthesis succeeds, and vice versa."""
    degraded_but_synthesized = SynthesizedAnswer(
        answer="body",
        fallback_used=True,
        fallback_reason="qmd_unavailable",
        synthesis_used=True,
    )
    assert degraded_but_synthesized.fallback_used is True
    assert degraded_but_synthesized.synthesis_used is True

    good_pages_no_synthesis = SynthesizedAnswer(
        answer="body",
        synthesis_used=False,
        synthesis_reason="RuntimeError: boom",
    )
    assert good_pages_no_synthesis.fallback_used is False
    assert good_pages_no_synthesis.synthesis_used is False


def test_synthesized_answer_carries_question_and_receipt() -> None:
    """``question`` is the prompt (slug source); ``file_receipt`` carries the F3 file-back result."""
    receipt = MemoryReceipt(
        changed_pages=[],
        deferred=[],
        fallback_used=False,
        fallback_reason="",
        errors=[],
    )
    ans = SynthesizedAnswer(
        question="what is a hook?",
        answer="...",
        file_receipt=receipt,
    )
    assert ans.question == "what is a hook?"
    assert ans.file_receipt is receipt


def test_synthesized_answer_default_file_receipt_is_none() -> None:
    """F4a call sites must keep working unchanged when ``file_receipt`` is omitted."""
    ans = SynthesizedAnswer(answer="...")
    assert ans.file_receipt is None


def test_synthesized_answer_default_question_is_empty_string() -> None:
    """``question`` defaults to ``""`` to preserve call sites that don't supply it."""
    ans = SynthesizedAnswer(answer="...")
    assert ans.question == ""
