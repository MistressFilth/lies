"""The CLI must say which engine wrote the answer when it is notable."""

from __future__ import annotations

from unittest import mock

from typer.testing import CliRunner

from lies.cli import app
from lies.query.models import SynthesizedAnswer

runner = CliRunner()


def _run(answer: SynthesizedAnswer) -> str:
    with (
        mock.patch("lies.cli.resolve_wiki"),
        mock.patch("lies.cli.Orchestrator") as orch_cls,
    ):
        orch_cls.return_value.run_query.return_value = answer
        result = runner.invoke(app, ["query", "what is alpha?", "--name", "w"])
    return result.stdout


def test_clean_synthesis_prints_no_note() -> None:
    out = _run(SynthesizedAnswer(answer="Alpha.", synthesis_used=True))
    assert "Note:" not in out


def test_failed_synthesis_prints_the_extractive_note() -> None:
    out = _run(
        SynthesizedAnswer(
            answer="Alpha.", synthesis_used=False, synthesis_reason="RuntimeError: boom"
        )
    )
    assert "LLM synthesis unavailable" in out
    assert "RuntimeError: boom" in out
    assert "extractively" in out


def test_successful_synthesis_with_a_note_prints_the_note_plainly() -> None:
    out = _run(
        SynthesizedAnswer(
            answer="Alpha.",
            synthesis_used=True,
            synthesis_reason="dropped 1 unretrieved citation(s): ghost.md",
        )
    )
    assert "dropped 1 unretrieved citation(s)" in out
    assert "unavailable" not in out
