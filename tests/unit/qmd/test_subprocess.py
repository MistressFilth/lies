"""Tests for the deadlock-free subprocess helper."""

from __future__ import annotations

import sys
from pathlib import Path


from lies.qmd._subprocess import _run_qmd


def test_run_qmd_returns_completed_process_on_success(tmp_path: Path):
    """A short-running process returns a CompletedProcess with stdout bytes."""
    script = tmp_path / "ok.py"
    script.write_text("import sys; sys.stdout.write('hello\\n'); sys.exit(0)\n")
    result = _run_qmd(
        [sys.executable, str(script)],
        cwd=tmp_path,
        timeout=5.0,
    )
    assert result.returncode == 0
    assert result.stdout == b"hello\n"
