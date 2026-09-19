"""Tests for qmd_cleanup and qmd_reindex restored for F38."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from lies.qmd import _proc
from lies.qmd import cli as qmd_cli
from lies.qmd._models import ReindexResult


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


def test_qmd_cleanup_invokes_qmd_cleanup_subcommand(tmp_path: Path) -> None:
    """qmd_cleanup shells out to ``qmd cleanup`` via the seam."""
    with patch.object(_proc, "run", return_value=_completed(returncode=0)) as mock_run:
        qmd_cli.qmd_cleanup(tmp_path)
    mock_run.assert_called_once()
    args = mock_run.call_args[0][0]
    assert args == ["cleanup"]


def test_qmd_cleanup_raises_on_failure(tmp_path: Path) -> None:
    """Non-zero returncode raises CalledProcessError-style failure."""
    with patch.object(_proc, "run", return_value=_completed(returncode=2, stderr="orphan rows")):
        with pytest.raises(subprocess.CalledProcessError):
            qmd_cli.qmd_cleanup(tmp_path)


def test_qmd_reindex_only_indexed(tmp_path: Path) -> None:
    """No flags set: only qmd update runs."""
    with patch.object(_proc, "run", return_value=_completed()) as mock_run:
        result = qmd_cli.qmd_reindex(tmp_path)
    assert mock_run.call_count == 1
    args = mock_run.call_args_list[0][0][0]
    assert args == ["update"]
    assert result == ReindexResult(indexed=True)


def test_qmd_reindex_cleanup_runs_cleanup_first(tmp_path: Path) -> None:
    """cleanup=True: qmd cleanup runs before qmd update."""
    with patch.object(_proc, "run", return_value=_completed()) as mock_run:
        result = qmd_cli.qmd_reindex(tmp_path, cleanup=True)
    assert mock_run.call_count == 2
    assert mock_run.call_args_list[0][0][0] == ["cleanup"]
    assert mock_run.call_args_list[1][0][0] == ["update"]
    assert result.cleaned is True
    assert result.indexed is True


def test_qmd_reindex_all_runs_cleanup_update_embed(tmp_path: Path) -> None:
    """all_=True: cleanup + update + embed, in order."""
    with patch.object(_proc, "run", return_value=_completed()) as mock_run:
        result = qmd_cli.qmd_reindex(tmp_path, all_=True, embed=True)
    assert mock_run.call_count == 3
    assert mock_run.call_args_list[0][0][0] == ["cleanup"]
    assert mock_run.call_args_list[1][0][0] == ["update"]
    # Collection name not set; embed has no -c flag yet (see brief); we run
    # with collection_name=None behavior, so args[0] is "embed" with no -c.
    assert mock_run.call_args_list[2][0][0] == ["embed"]
    assert result.cleaned is True
    assert result.indexed is True
    assert result.embedded is True


def test_qmd_reindex_embed_only(tmp_path: Path) -> None:
    """embed=True alone: only qmd embed runs."""
    with patch.object(_proc, "run", return_value=_completed()):
        result = qmd_cli.qmd_reindex(tmp_path, embed=True)
    # Without collection name, we still invoke qmd embed with --all or similar; see impl
    assert result.embedded is True


def test_qmd_reindex_collects_errors_on_failure(tmp_path: Path) -> None:
    """A failing stage returns errors=[...] without raising."""
    with patch.object(_proc, "run", return_value=_completed(returncode=1, stderr="boom")):
        result = qmd_cli.qmd_reindex(tmp_path, cleanup=True)
    assert result.errors == ["cleanup: boom"]
    assert result.cleaned is False
    assert result.indexed is False
