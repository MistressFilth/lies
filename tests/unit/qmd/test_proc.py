"""Tests for the _qmd_proc subprocess seam.

The seam wraps :func:`lies.qmd._subprocess._run_qmd` (the deadlock-
free helper introduced by Spec A of the qmd-drain plan) so qmd
library functions can be tested without shelling out to a real
``qmd`` binary. Tests for ``qmd_cleanup`` and ``qmd_reindex`` (F38)
patch ``_qmd_proc.run`` to exercise the per-stage sequencing, error
handling, and the force cache-wipe path without invoking a real qmd
binary.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from lies.qmd import _proc


def test_run_invokes_run_qmd_with_args_and_cwd(tmp_path: Path) -> None:
    """Passes args + cwd + timeout to :func:`lies.qmd._subprocess._run_qmd`."""
    fake = subprocess.CompletedProcess(args=["qmd", "status"], returncode=0, stdout=b"", stderr=b"")
    with patch("lies.qmd._proc._run_qmd", return_value=fake) as mock_run:
        result = _proc.run(["status"], cwd=tmp_path, timeout=10)
    mock_run.assert_called_once()
    args, kwargs = mock_run.call_args
    assert args[0] == ["qmd", "status"]
    assert kwargs["cwd"] == tmp_path
    assert kwargs["timeout"] == 10
    # Decode bytes -> str so the caller's text contract is preserved.
    assert result is mock_run.return_value or result.returncode == fake.returncode
    assert isinstance(result.stdout, str)
    assert isinstance(result.stderr, str)


def test_run_returns_completed_process_on_nonzero_returncode(tmp_path: Path) -> None:
    """Returns the CompletedProcess even when returncode != 0 (caller decides)."""
    fake = subprocess.CompletedProcess(args=["qmd", "bad"], returncode=2, stdout=b"", stderr=b"err")
    with patch("lies.qmd._proc._run_qmd", return_value=fake):
        result = _proc.run(["bad"], cwd=tmp_path)
    assert result.returncode == 2
    assert result.stderr == "err"
