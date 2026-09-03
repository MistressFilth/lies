"""CLI tests for ``lies query --collection/--no-file/--force-file`` (F3).

The four new flags surface Task 4's file-back plumbing on the CLI:

- ``--collection NAME`` forwards ``collection=NAME`` to the orchestrator so
  the synthesized answer lands under ``wiki/<NAME>/synthesis/<file>``.
- ``--no-file`` overrides the agent's ``should_file`` verdict by passing
  ``file=False``; no write happens and no receipt is printed.
- ``--force-file`` flips ``file_back_synthesis`` on even when the agent
  judged the answer unworthy of a page.
- The receipt is rendered as ``(synthesis: durably filed - <op>: <path>)``
  on success, ``(synthesis: error — <first error>)`` on failure, and
  silently omitted when both ``changed_pages`` and ``errors`` are empty.

The brief's "missing collection should exit 2" sketch doesn't match the
current orchestrator contract: ``run_query`` degrades gracefully by
appending ``"not filed: --collection required"`` to ``synthesis_reason``
and returns the unfilled answer, which the CLI prints through the
existing receipt path. We assert that annotation here instead of
synthesising an exit code.
"""

from __future__ import annotations

from unittest import mock

from typer.testing import CliRunner

from lies.cli import app
from lies.memory.models import MemoryReceipt, OperationKind, PageReference
from lies.query.models import SynthesizedAnswer

runner = CliRunner()


def _run_query_call(*args: str) -> mock.Mock:
    """Invoke ``lies query`` with a fake orchestrator and return ``run_query``'s call.

    Mocks ``resolve_wiki`` so no on-disk wiki is needed. The orchestrator
    returns a clean ``SynthesizedAnswer`` so the command body succeeds;
    tests assert on the kwargs the CLI forwarded.
    """
    with (
        mock.patch("lies.cli.resolve_wiki"),
        mock.patch("lies.cli.Orchestrator") as orch_cls,
    ):
        orch_cls.return_value.run_query.return_value = SynthesizedAnswer(answer="X.")
        result = runner.invoke(app, ["query", "what is alpha?", "--name", "w", *args])
        call = orch_cls.return_value.run_query.call_args
    assert result.exit_code == 0, result.stdout
    assert call is not None, "run_query was never invoked"
    return call


def test_query_no_file_skips_file_back() -> None:
    """``--no-file`` overrides ``should_file``; CLI passes ``file=False`` to orchestrator."""
    call = _run_query_call("--no-file")
    assert call.kwargs["file"] is False
    assert call.kwargs["force_file"] is False
    assert call.kwargs["collection"] is None


def test_query_force_file_writes_even_when_should_file_false() -> None:
    """``--force-file`` flips ``file_back_synthesis`` on independent of ``should_file``."""
    call = _run_query_call("--force-file")
    assert call.kwargs["force_file"] is True
    # file defaults to True; collection is None until the operator supplies one.
    assert call.kwargs["file"] is True
    assert call.kwargs["collection"] is None


def test_query_collection_passes_through() -> None:
    """``--collection NAME`` forwards ``collection=NAME`` to the orchestrator."""
    call = _run_query_call("--collection", "claude-code")
    assert call.kwargs["collection"] == "claude-code"


def test_query_default_collection_file_and_force_file() -> None:
    """Without any of the three flags, defaults match Task 4's signature."""
    call = _run_query_call()
    assert call.kwargs["collection"] is None
    assert call.kwargs["file"] is True
    assert call.kwargs["force_file"] is False


def test_query_missing_collection_when_should_file_annotates_reason() -> None:
    """Missing collection with ``should_file=True`` records a synthesis_reason note.

    The orchestrator degrades gracefully rather than raising, so the CLI
    must not invent an exit code — it just prints the annotated note via
    the existing receipt path.
    """
    with (
        mock.patch("lies.cli.resolve_wiki"),
        mock.patch("lies.cli.Orchestrator") as orch_cls,
    ):
        orch_cls.return_value.run_query.return_value = SynthesizedAnswer(
            answer="X.",
            synthesis_used=True,
            should_file=True,
            synthesis_reason="not filed: --collection required",
        )
        result = runner.invoke(app, ["query", "what is alpha?", "--name", "w"])

    assert result.exit_code == 0, result.stdout
    assert "not filed: --collection required" in result.stdout


def test_query_success_receipt_prints_synthesis_line() -> None:
    """On file-back success the receipt prints the ``(synthesis: ...)`` line."""
    receipt = MemoryReceipt(
        changed_pages=[
            PageReference(
                path="claude-code/synthesis/x.md",
                collection_id="claude-code",
                op=OperationKind.CREATE,
            )
        ],
        deferred=[],
        fallback_used=False,
        fallback_reason="",
        errors=[],
    )
    with (
        mock.patch("lies.cli.resolve_wiki"),
        mock.patch("lies.cli.Orchestrator") as orch_cls,
    ):
        orch_cls.return_value.run_query.return_value = SynthesizedAnswer(
            answer="X.",
            should_file=True,
            file_receipt=receipt,
        )
        result = runner.invoke(
            app, ["query", "what?", "--name", "w", "--collection", "claude-code"]
        )

    assert result.exit_code == 0, result.stdout
    assert "(synthesis: durably filed" in result.stdout
    assert "create: claude-code/synthesis/x.md" in result.stdout


def test_query_error_receipt_prints_error_line() -> None:
    """On file-back failure the receipt prints the ``(synthesis: error ...)`` line."""
    receipt = MemoryReceipt(
        changed_pages=[],
        deferred=[],
        fallback_used=False,
        fallback_reason="",
        errors=["file_back_failed_after_3_attempts: WikiLockBusy: busy"],
    )
    with (
        mock.patch("lies.cli.resolve_wiki"),
        mock.patch("lies.cli.Orchestrator") as orch_cls,
    ):
        orch_cls.return_value.run_query.return_value = SynthesizedAnswer(
            answer="X.",
            should_file=True,
            file_receipt=receipt,
        )
        result = runner.invoke(
            app, ["query", "what?", "--name", "w", "--collection", "claude-code"]
        )

    assert result.exit_code == 0, result.stdout
    assert "(synthesis: error" in result.stdout
    assert "file_back_failed_after_3_attempts" in result.stdout


def test_query_empty_receipt_prints_no_synthesis_line() -> None:
    """An empty ``file_receipt`` (no changes, no errors) prints nothing new."""
    receipt = MemoryReceipt(
        changed_pages=[],
        deferred=[],
        fallback_used=False,
        fallback_reason="",
        errors=[],
    )
    with (
        mock.patch("lies.cli.resolve_wiki"),
        mock.patch("lies.cli.Orchestrator") as orch_cls,
    ):
        orch_cls.return_value.run_query.return_value = SynthesizedAnswer(
            answer="X.",
            should_file=False,
            file_receipt=receipt,
        )
        result = runner.invoke(app, ["query", "what?", "--name", "w"])

    assert result.exit_code == 0, result.stdout
    assert "(synthesis:" not in result.stdout
