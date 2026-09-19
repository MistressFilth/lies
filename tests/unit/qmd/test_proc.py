"""Tests for the _qmd_proc subprocess seam.

The seam wraps ``subprocess.run`` so qmd library functions can be
tested without shelling out to a real ``qmd`` binary. Future tasks
(``qmd_cleanup``, ``qmd_reindex``) will mock ``_qmd_proc.run``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from lies.qmd import _proc


def test_run_invokes_subprocess_run_with_args_and_cwd(tmp_path: Path) -> None:
    """Passes args + cwd + timeout to ``subprocess.run``."""
    fake = subprocess.CompletedProcess(args=["qmd", "status"], returncode=0, stdout="", stderr="")
    with patch("lies.qmd._proc.subprocess.run", return_value=fake) as mock_run:
        result = _proc.run(["status"], cwd=tmp_path, timeout=10)
    mock_run.assert_called_once()
    args, kwargs = mock_run.call_args
    assert args[0] == ["status"]
    assert kwargs["cwd"] == tmp_path
    assert kwargs["timeout"] == 10
    assert kwargs["check"] is False
    assert result is fake


def test_run_raises_on_nonzero_returncode(tmp_path: Path) -> None:
    """Returns the CompletedProcess even when returncode != 0 (caller decides)."""
    fake = subprocess.CompletedProcess(args=["qmd", "bad"], returncode=2, stdout="", stderr="err")
    with patch("lies.qmd._proc.subprocess.run", return_value=fake):
        result = _proc.run(["bad"], cwd=tmp_path)
    assert result.returncode == 2
    assert result.stderr == "err"
