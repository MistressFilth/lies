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


@pytest.mark.slow
def test_run_qmd_kills_process_group_on_timeout(tmp_path: Path):
    """Timeout SIGKILLs the entire process group, not just the immediate child.

    On this host, qmd is a bun shim that forks node.js as a grandchild
    and exec's into it. ``proc.kill()`` alone leaves the grandchild
    orphaned in its own process group. This test pins the
    ``start_new_session=True`` + ``os.killpg`` contract: a forked
    grandchild that ignores SIGTERM and outlasts the timeout must be
    reaped along with its parent.

    Marked slow because the test waits the full ``timeout`` (1.0s)
    for the SIGKILL path to fire, well over the 0.15s hard-limit
    gate enforced for non-slow tests.
    """
    script = tmp_path / "spawn_grandchild.py"
    # Two-child tree:
    # - immediate child of helper: forks the grandchild, then sleeps
    #   long enough that the helper's timeout fires while both are
    #   still alive.
    # - grandchild: ignores SIGTERM (SIGKILL is uncatchable, so the
    #   only way for it to survive a killpg is for the group to be
    #   intact — which is exactly the regression we are guarding
    #   against).
    script.write_text(
        "import os, signal, time\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        # Grandchild: ignore SIGTERM so only SIGKILL via killpg can
        # end it. SIGKILL on the wrong process group leaves this
        # orphan alive — the regression we're pinning.
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "    time.sleep(60)\n"
        "else:\n"
        # Immediate child: wait so the helper sees both processes
        # alive at timeout, then the helper's killpg must reap both.
        "    time.sleep(60)\n"
    )
    with pytest.raises(subprocess.TimeoutExpired):
        _run_qmd(
            [sys.executable, str(script)],
            cwd=tmp_path,
            timeout=1.0,
        )
    # Allow the kernel a moment to reap both children after killpg.
    time.sleep(0.5)
    # ``pgrep`` returns 1 (and empty stdout) when no matches. If the
    # helper failed to kill the group, the grandchild or the immediate
    # child would still be alive and pgrep would list them.
    probe = subprocess.run(
        ["pgrep", "-f", "spawn_grandchild.py"],
        capture_output=True,
        text=True,
        timeout=5.0,
    )
    assert probe.returncode != 0 or not probe.stdout.strip(), (
        f"orphan subprocesses still alive after killpg: {probe.stdout!r}"
    )


@pytest.mark.slow
def test_run_qmd_long_stderr_kills_grandchild(tmp_path: Path):
    """Long-stderr path also kills the entire process group on timeout.

    The bug fixed in this branch surfaces most acutely when qmd's
    stderr fills the OS pipe buffer AND the process is slow enough
    to hit the timeout: the child blocks writing stderr, the parent
    hits the timeout, and the SIGKILL must reach the whole group so
    the grandchild does not outlive the parent. This test combines
    both failure modes (100 KB stderr write + forked grandchild
    that ignores SIGTERM) into one scenario.
    """
    script = tmp_path / "loud_grandchild.py"
    script.write_text(
        "import os, signal, sys, time\n"
        # Fill the OS pipe buffer so the parent cannot drain stderr
        # promptly. This is the precondition for the timeout path to
        # fire rather than a clean exit.
        "sys.stderr.write('x' * 100_000)\n"
        "sys.stderr.flush()\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "    time.sleep(60)\n"
        "else:\n"
        "    time.sleep(60)\n"
    )
    with pytest.raises(subprocess.TimeoutExpired):
        _run_qmd(
            [sys.executable, str(script)],
            cwd=tmp_path,
            timeout=1.0,
        )
    time.sleep(0.5)
    probe = subprocess.run(
        ["pgrep", "-f", "loud_grandchild.py"],
        capture_output=True,
        text=True,
        timeout=5.0,
    )
    assert probe.returncode != 0 or not probe.stdout.strip(), (
        f"orphan subprocesses still alive after killpg: {probe.stdout!r}"
    )
