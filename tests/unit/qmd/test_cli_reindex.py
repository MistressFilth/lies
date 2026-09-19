"""Tests for qmd_cleanup and qmd_reindex restored for F38."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from lies.qmd import _proc
from lies.qmd import cli as qmd_cli


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
