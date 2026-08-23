"""Unit tests for lies.qmd.cli collection helpers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lies.qmd import cli as qmd_cli


def _run_record(commands: list[list[str]]):
    """Build a fake ``_run`` that records commands and returns canned responses.

    Each entry in ``commands`` is paired with a response: ``commands[i]``
    is the args list the test expects for the i-th call; the matching
    response (a CompletedProcess-like MagicMock) is supplied via the
    parallel ``responses`` list at call time. Tests construct the
    closures inline.
    """

    recorded: list[list[str]] = []

    def make(responses):
        def fake(args, cwd, timeout=300):
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
    make, recorded = _run_record(responses)
    monkeypatch.setattr(qmd_cli, "_run", make(responses))

    info = qmd_cli.qmd_collection_show(tmp_path, "claude_code")
    assert info == {"path": "/home/divinefilth/.local/share/lies/ingested/lies/claude_code/wiki"}
    assert recorded == [["collection", "show", "claude_code"]]


def test_qmd_collection_show_returns_none_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """qmd exits non-zero (collection not found) -> None, not exception."""
    responses = [_completed(1, stderr="collection not found")]
    make, recorded = _run_record(responses)
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
    make, recorded = _run_record(responses)
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
    make, recorded = _run_record(responses)
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
    make, recorded = _run_record(responses)
    monkeypatch.setattr(qmd_cli, "_run", make(responses))

    new_path = tmp_path / "wiki"
    qmd_cli.qmd_collection_add_or_update(tmp_path, new_path, "claude_code")
    assert recorded == [
        ["collection", "show", "claude_code"],
        ["collection", "add", str(new_path), "--name", "claude_code"],
    ]


def test_qmd_collection_add_or_update_remove_failure_still_adds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """remove() non-zero -> continue with add anyway (best-effort)."""
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
    make, recorded = _run_record(responses)
    monkeypatch.setattr(qmd_cli, "_run", make(responses))

    new_path = tmp_path / "wiki"
    qmd_cli.qmd_collection_add_or_update(tmp_path, new_path, "claude_code")
    assert recorded[0] == ["collection", "show", "claude_code"]
    assert recorded[1] == ["collection", "remove", "claude_code"]
    assert recorded[2] == ["collection", "add", str(new_path), "--name", "claude_code"]


def test_qmd_embed_invokes_per_collection_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-collection embed passes -c <name>."""
    responses = [_completed(0)]
    make, recorded = _run_record(responses)
    monkeypatch.setattr(qmd_cli, "_run", make(responses))

    qmd_cli.qmd_embed(tmp_path, "claude_code")
    assert recorded == [["embed", "-c", "claude_code"]]


def test_qmd_embed_default_timeout_is_thirty_minutes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default timeout = 1800 s. Override path takes the kwarg value."""
    seen_timeouts: list[int] = []
    recorded: list[list[str]] = []

    def fake(args, cwd, timeout=300):
        recorded.append(list(args))
        seen_timeouts.append(timeout)
        return _completed(0)

    monkeypatch.setattr(qmd_cli, "_run", fake)
    qmd_cli.qmd_embed(tmp_path, "claude_code")
    assert seen_timeouts == [1800]

    qmd_cli.qmd_embed(tmp_path, "claude_code", timeout=42)
    assert seen_timeouts == [1800, 42]


def test_qmd_embed_propagates_qmd_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-zero exit raises QmdError (the post-commit hook in write.py catches it)."""
    responses = [_completed(1, stderr="model not pulled")]
    make, _ = _run_record(responses)
    monkeypatch.setattr(qmd_cli, "_run", make(responses))

    with pytest.raises(qmd_cli.QmdError):
        qmd_cli.qmd_embed(tmp_path, "claude_code")
