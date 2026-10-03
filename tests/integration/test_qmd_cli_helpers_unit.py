"""Unit tests for lies.qmd.cli collection helpers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import subprocess

import pytest

from lies.qmd import cli as qmd_cli


def _run_record():
    """Build a fake ``_run`` that records commands and returns canned responses.

    ``make(responses)`` returns a closure that returns ``responses[i]``
    for the i-th call and records the args list; tests construct the
    closures inline.
    """

    recorded: list[list[str]] = []

    def make(responses):
        def fake(args, cwd, timeout=300, *, idle_timeout=None):
            idx = len(recorded)
            recorded.append(list(args))
            if idx >= len(responses):
                raise AssertionError(f"_run called {idx + 1} times, expected {len(responses)}")
            return responses[idx]

        return fake

    return make, recorded


def _completed(returncode: int, stdout: str = "", stderr: str = ""):
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


_SHOW_OUTPUT_MATCH = (
    "Collection: claude_code\n"
    "  Path:     /home/divinefilth/.local/share/lies/ingested/lies/claude_code/wiki\n"
    "  Pattern:  **/*.md\n"
    "  Include:  yes (default)\n"
)


def test_qmd_collection_show_parses_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy path: parses the Path: line out of qmd collection show output."""
    responses = [_completed(0, stdout=_SHOW_OUTPUT_MATCH)]
    make, recorded = _run_record()
    monkeypatch.setattr(qmd_cli, "_run", make(responses))

    info = qmd_cli.qmd_collection_show(tmp_path, "claude_code")
    assert info == {"path": "/home/divinefilth/.local/share/lies/ingested/lies/claude_code/wiki"}
    assert recorded == [["collection", "show", "claude_code"]]


def test_qmd_collection_show_returns_none_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """qmd exits non-zero (collection not found) -> None, not exception."""
    responses = [_completed(1, stderr="collection not found")]
    make, recorded = _run_record()
    monkeypatch.setattr(qmd_cli, "_run", make(responses))

    assert qmd_cli.qmd_collection_show(tmp_path, "missing") is None
    assert recorded == [["collection", "show", "missing"]]


def test_qmd_collection_add_or_update_noop_when_path_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """show() returns the same path -> only one subprocess call (the show)."""
    expected = str(tmp_path / "wiki")
    responses = [
        _completed(
            0,
            stdout=(
                "Collection: claude_code\n"
                f"  Path:     {expected}\n"
                "  Pattern:  **/*.md\n"
                "  Include:  yes (default)\n"
            ),
        )
    ]
    make, recorded = _run_record()
    monkeypatch.setattr(qmd_cli, "_run", make(responses))

    qmd_cli.qmd_collection_add_or_update(tmp_path, Path(expected), "claude_code")
    # show only; no remove, no add.
    assert recorded == [["collection", "show", "claude_code"]]


def test_qmd_collection_add_or_update_readds_when_path_differs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """show() returns a stale path -> remove + add, in that order."""
    responses = [
        _completed(
            0,
            stdout=(
                "Collection: claude_code\n"
                "  Path:     /old/path\n"
                "  Pattern:  **/*.md\n"
                "  Include:  yes (default)\n"
            ),
        ),
        _completed(0),  # remove
        _completed(0),  # add
    ]
    make, recorded = _run_record()
    monkeypatch.setattr(qmd_cli, "_run", make(responses))

    new_path = tmp_path / "wiki"
    qmd_cli.qmd_collection_add_or_update(tmp_path, new_path, "claude_code")
    assert recorded == [
        ["collection", "show", "claude_code"],
        ["collection", "remove", "claude_code"],
        ["collection", "add", str(new_path), "--name", "claude_code"],
    ]


def test_qmd_collection_add_or_update_adds_when_show_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """show() returns None (qmd exit != 0) -> straight to add."""
    responses = [
        _completed(1, stderr="not found"),  # show fails
        _completed(0),  # add
    ]
    make, recorded = _run_record()
    monkeypatch.setattr(qmd_cli, "_run", make(responses))

    new_path = tmp_path / "wiki"
    qmd_cli.qmd_collection_add_or_update(tmp_path, new_path, "claude_code")
    assert recorded == [
        ["collection", "show", "claude_code"],
        ["collection", "add", str(new_path), "--name", "claude_code"],
    ]


def test_qmd_collection_add_or_update_remove_failure_still_adds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """remove() non-zero -> continue with add anyway (best-effort) and warn on stderr."""
    responses = [
        _completed(
            0,
            stdout=(
                "Collection: claude_code\n"
                "  Path:     /old/path\n"
                "  Pattern:  **/*.md\n"
                "  Include:  yes (default)\n"
            ),
        ),
        _completed(1, stderr="remove failed"),  # remove fails
        _completed(0),  # add still runs
    ]
    make, recorded = _run_record()
    monkeypatch.setattr(qmd_cli, "_run", make(responses))

    new_path = tmp_path / "wiki"
    qmd_cli.qmd_collection_add_or_update(tmp_path, new_path, "claude_code")
    assert recorded[0] == ["collection", "show", "claude_code"]
    assert recorded[1] == ["collection", "remove", "claude_code"]
    assert recorded[2] == ["collection", "add", str(new_path), "--name", "claude_code"]
    err = capsys.readouterr().err
    assert "qmd collection remove claude_code failed" in err
    assert "remove failed" in err


def test_qmd_embed_invokes_per_collection_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-collection embed passes -c <name>."""
    responses = [_completed(0)]
    make, recorded = _run_record()
    monkeypatch.setattr(qmd_cli, "_run", make(responses))

    qmd_cli.qmd_embed(tmp_path, "claude_code")
    assert recorded == [["embed", "-c", "claude_code"]]


def test_qmd_embed_default_timeout_is_thirty_minutes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default timeout = 1800 s. Override path takes the kwarg value.

    The idle bound tracks the total bound rather than staying at the 30 s
    default, because ``qmd embed`` is silent for its entire duration under a
    pipe: it writes one stderr spinner escape and then nothing while the
    model loads and runs, so the silence is the operation, not a wedge. A
    30 s idle bound killed it mid-progress (measured: 9.4 s of unbroken
    silence for one tiny document on a cold cache; four collections under
    host contention crossed 30 s). Retrieval commands keep the default — a
    query silent for 30 s genuinely is wedged.

    It tracks at a **fraction**, not at 1.0: the reader loop checks the total
    bound first, so an idle bound equal to the total could never fire and
    every kill would lose its ``last_output`` diagnostic. See
    ``SILENT_COMMAND_IDLE_TIMEOUT_FRACTION``.
    """
    seen_timeouts: list[int] = []
    seen_idles: list[float | None] = []
    recorded: list[list[str]] = []

    def fake(args, cwd, timeout=300, *, idle_timeout=None):
        recorded.append(list(args))
        seen_timeouts.append(timeout)
        seen_idles.append(idle_timeout)
        return _completed(0)

    monkeypatch.setattr(qmd_cli, "_run", fake)
    qmd_cli.qmd_embed(tmp_path, "claude_code")
    assert seen_timeouts == [1800]
    half = qmd_cli.SILENT_COMMAND_IDLE_TIMEOUT_FRACTION
    assert seen_idles == [1800.0 * half], "embed's idle bound must follow its total bound"

    qmd_cli.qmd_embed(tmp_path, "claude_code", timeout=42)
    assert seen_timeouts == [1800, 42]
    assert seen_idles == [1800.0 * half, 42.0 * half], (
        "the idle bound must track an overridden timeout too"
    )


def test_run_keeps_the_default_idle_bound_for_other_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Commands that are not embed leave the 30 s default in place.

    The companion to the test above: raising embed's idle bound must not
    silently raise it for everything. ``_run`` passes
    ``DEFAULT_IDLE_TIMEOUT_S`` when the caller does not override, so a
    retrieval call keeps the bound that catches a real wedge.
    """
    seen: list[float] = []

    def fake(args, cwd, timeout, idle_timeout):
        seen.append(idle_timeout)
        # ``_run_qmd`` returns bytes; ``_run`` decodes them.
        return subprocess.CompletedProcess(args, 0, stdout=b"[]", stderr=b"")

    monkeypatch.setattr(qmd_cli, "_run_qmd", fake)
    monkeypatch.setattr(qmd_cli.shutil, "which", lambda _name: "/usr/bin/qmd")

    result = qmd_cli._run(["query", "x"], cwd=tmp_path, timeout=60)

    assert seen == [qmd_cli.DEFAULT_IDLE_TIMEOUT_S]
    assert result.returncode == 0


def test_qmd_embed_propagates_qmd_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-zero exit raises QmdError (the post-commit hook in write.py catches it)."""
    responses = [_completed(1, stderr="model not pulled")]
    make, _ = _run_record()
    monkeypatch.setattr(qmd_cli, "_run", make(responses))

    with pytest.raises(qmd_cli.QmdError):
        qmd_cli.qmd_embed(tmp_path, "claude_code")
