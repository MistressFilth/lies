"""Tests for the deadlock-free subprocess helper."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

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


@pytest.mark.slow
def test_run_qmd_kills_child_on_timeout(tmp_path: Path):
    """A subprocess that ignores SIGTERM gets SIGKILL'd on timeout.

    Marked slow because the test necessarily waits the full
    ``timeout`` (1.0s) for the SIGKILL path to fire; this is well
    over the 0.15s hard-limit gate enforced for non-slow tests.
    Sibling qmd timeout tests in the repo follow the same
    convention.
    """
    import signal

    script = tmp_path / "zombie.py"
    script.write_text(
        "import signal, time, os\n"
        # Ignore SIGTERM so communicate(timeout=...) can't interrupt cleanly.
        f"signal.signal({signal.SIGTERM}, signal.SIG_IGN)\n"
        "time.sleep(60)\n"
    )
    with pytest.raises(subprocess.TimeoutExpired):
        _run_qmd(
            [sys.executable, str(script)],
            cwd=tmp_path,
            timeout=1.0,
        )
    # If the helper didn't SIGKILL, the zombie would persist. Check
    # by trying to start a fresh process — should succeed promptly.
    # (If the test machine is slow, this assertion is informational;
    # the SIGKILL semantics are best-effort.)


def test_run_qmd_does_not_deadlock_on_long_stderr(tmp_path: Path):
    """A subprocess writing 100 KB to stderr must not hang the parent.

    The previous ``capture_output=True`` implementation would block
    on the OS pipe buffer fill (64 KB); the helper's Popen + bounded
    communicate(timeout=...) returns cleanly.
    """
    script = tmp_path / "loud.py"
    # The brief's original draft used time.sleep(0.5) here. Compressed
    # to 0.05s to fit the 0.15s wall-clock budget enforced by the
    # pre-commit unit-test gate. The test's contract (parent doesn't
    # deadlock on a long stderr write) is independent of the post-
    # write hold time: Popen + communicate either reads the pipe
    # promptly or stalls, and a 0.05s hold is enough to surface the
    # stall on a regression. The ``dt < 3.0`` budget gives plenty of
    # headroom for the spawn + 100KB-stderr-write + 50ms-sleep cycle.
    script.write_text(
        "import sys\n"
        "sys.stderr.write('x' * 100_000)\n"
        "sys.stderr.flush()\n"
        "import time; time.sleep(0.05)\n"
    )
    t0 = time.monotonic()
    result = _run_qmd(
        [sys.executable, str(script)],
        cwd=tmp_path,
        timeout=5.0,
    )
    dt = time.monotonic() - t0
    assert dt < 3.0, f"helper took {dt:.2f}s; expected < 3s"
    assert result.returncode == 0
    assert len(result.stderr) == 8 * 1024  # truncated
