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

``--force-file`` (or an agent that marked ``should_file=True``) without
``--collection`` is a hard error: the CLI exits 2 with the spec'd
``error: --collection NAME required to file synthesis (or pass --no-file to skip)``
message, mirroring the spec's typed-error contract.
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


def test_query_missing_collection_with_force_file_exits_2() -> None:
    """Missing collection with ``--force-file`` raises ``WikiPlanInvalid`` → exit 2.

    The orchestrator raises :class:`WikiPlanInvalid` when the caller
    asks for a filing (``file=True`` and ``should_file=True`` /
    ``force_file=True``) but does not supply ``--collection``. The CLI
    catches the typed error, prints the spec'd message on stderr, and
    exits 2 so operators can script the failure.
    """
    from lies.memory.models import WikiPlanInvalid

    with (
        mock.patch("lies.cli.resolve_wiki"),
        mock.patch("lies.cli.Orchestrator") as orch_cls,
    ):
        orch_cls.return_value.run_query.side_effect = WikiPlanInvalid(
            "collection required to file synthesis"
        )
        result = runner.invoke(app, ["query", "what is alpha?", "--name", "w", "--force-file"])

    assert result.exit_code == 2, result.stdout
    assert "--collection NAME required to file synthesis" in result.output
    assert "--no-file" in result.output


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
