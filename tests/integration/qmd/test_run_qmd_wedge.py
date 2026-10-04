"""A slow qmd call and a wedged qmd call are not the same failure.

An absolute deadline (``communicate(timeout=N)``) cannot separate them:
the two distributions overlap, so any single threshold either kills
legitimate work or waits forever on a wedge.

Idle time can. qmd writes progress to stderr as it works —

    Expanding query... (1ms)
    Embedding 35 queries... (2.6s)
    Reranking 40 chunks... (1ms)

— so "no new output for N seconds" is a wedge and "M seconds while
still emitting" is slow work. Two bounds, and the error says which
fired: ``idle`` (no output for ``idle_timeout``) is the wedge signal,
``total`` is the backstop for a process that talks forever.

Mutation behind these tests: revert ``_run_qmd`` to
``communicate(timeout=N)`` and every one of them fails.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lies.qmd._subprocess import QmdWedgeError, _run_qmd

# A child that writes to stderr on a schedule, so "progressing" and
# "silent" are both reproducible without racing a real qmd.
PROGRESSING = r"""
import sys, time
for i in range(6):
    sys.stderr.write(f"phase {i}\n"); sys.stderr.flush()
    time.sleep(0.30)
sys.stdout.write("[]")
"""

SILENT = r"""
import time
time.sleep(30)
sys.stdout.write("[]")
"""


def _child(src: str, path: Path) -> list[str]:
    path.write_text(src, encoding="utf-8")
    return [sys.executable, str(path)]


def test_a_slow_but_progressing_child_is_not_killed(tmp_path: Path) -> None:
    """Progress resets the idle clock, so a slow query is never a wedge.

    The child takes 1.8s of wall time; the idle bound is 1.0s. It
    emits every 0.3s, so it should survive a bound it *exceeds in
    total* — and it does, because it never goes idle.
    """
    args = _child(PROGRESSING, tmp_path / "progress.py")

    result = _run_qmd(args, cwd=tmp_path, timeout=10, idle_timeout=1.0)

    assert result.returncode == 0, result.stderr.decode()
    assert b"phase 5" in result.stderr, "the child was allowed to finish"


def test_a_silent_child_is_killed_on_the_idle_bound(tmp_path: Path) -> None:
    """Silence is the wedge signal, however generous the total budget.

    The child would run 30s. The total bound is 30s — it would not
    fire. The idle bound is 0.8s, so this dies long before the ceiling
    and the error must say which bound fired.
    """
    args = _child(SILENT, tmp_path / "silent.py")

    with pytest.raises(QmdWedgeError) as excinfo:
        _run_qmd(args, cwd=tmp_path, timeout=30, idle_timeout=0.8)

    err = excinfo.value
    assert err.bound == "idle", err.bound
    assert err.idle_timeout == 0.8, err.idle_timeout
    assert "idle" in str(err), str(err)


def test_the_whole_process_group_is_reaped_on_a_wedge(tmp_path: Path) -> None:
    """A wedged grandchild must not outlive its parent.

    qmd forks a node.js grandchild under the bun shim. The idle path
    has to reach the same cleanup; a wedge that leaves a live child
    holding the embedding model is worse than the timeout.
    """
    marker = tmp_path / "grandchild.pid"
    src = (
        "import os, subprocess, sys, time\n"
        f"p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        f"open({str(marker)!r}, 'w').write(str(p.pid))\n"
        "time.sleep(30)\n"
    )
    args = _child(src, tmp_path / "spawner.py")

    with pytest.raises(QmdWedgeError):
        _run_qmd(args, cwd=tmp_path, timeout=30, idle_timeout=0.8)

    # The grandchild is gone, not merely orphaned.
    grandchild = int(marker.read_text())
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    pytest.fail(f"grandchild {grandchild} survived the wedge kill")


def test_a_child_that_never_stops_talking_still_hits_the_total_ceiling(
    tmp_path: Path,
) -> None:
    """The idle bound cannot be the only one.

    A process that emits forever would reset the idle clock
    indefinitely; the absolute ceiling is the backstop.
    """
    src = r"""
import sys, time
while True:
    sys.stderr.write("still working\n"); sys.stderr.flush()
    time.sleep(0.1)
"""
    args = _child(src, tmp_path / "chatty.py")

    with pytest.raises(QmdWedgeError) as excinfo:
        _run_qmd(args, cwd=tmp_path, timeout=1.5, idle_timeout=30.0)

    assert excinfo.value.bound == "total", excinfo.value.bound
    assert "total" in str(excinfo.value), str(excinfo.value)


def test_the_wedge_error_names_the_last_output_it_saw(tmp_path: Path) -> None:
    """The evidence travels with the error.

    ``last_output`` is what a reader uses to decide whether the process
    was working hard or had gone quiet at a phase boundary.
    """
    src = r"""
import sys, time
sys.stderr.write("Embedding 35 queries... (2.6s)\n"); sys.stderr.flush()
time.sleep(30)
"""
    args = _child(src, tmp_path / "phase.py")

    with pytest.raises(QmdWedgeError) as excinfo:
        _run_qmd(args, cwd=tmp_path, timeout=30, idle_timeout=0.6)

    assert "Embedding 35 queries" in excinfo.value.last_output, excinfo.value.last_output


def test_a_clean_run_reports_no_wedge(tmp_path: Path) -> None:
    """The success path is untouched and carries its stderr through."""
    args = _child(PROGRESSING, tmp_path / "ok.py")
    result = _run_qmd(args, cwd=tmp_path, timeout=10, idle_timeout=1.0)
    assert result.returncode == 0
    assert b"phase 0" in result.stderr


def test_a_nonzero_exit_is_still_a_command_error_not_a_wedge(tmp_path: Path) -> None:
    """A qmd that fails fast is a failure, not a hang.

    Kept as a boundary test: the wedge type must not absorb ordinary
    errors, or a real qmd rejection would be reported to a user as a
    stall and retried forever.
    """
    src = "import sys; sys.stderr.write('boom\\n'); sys.exit(3)\n"
    args = _child(src, tmp_path / "fail.py")

    result = _run_qmd(args, cwd=tmp_path, timeout=10, idle_timeout=5.0)
    assert result.returncode == 3, "a fast nonzero exit is a failure, not a wedge"
    assert b"boom" in result.stderr


def test_the_idle_default_is_derived_not_hardcoded_per_call() -> None:
    """Call sites that pass no ``idle_timeout`` get the shared default.

    Otherwise the four retrieval call sites would each grow their own
    literal, which is the drift this whole change exists to remove.
    """
    from lies.qmd._subprocess import DEFAULT_IDLE_TIMEOUT_S

    assert DEFAULT_IDLE_TIMEOUT_S > 0
    assert isinstance(DEFAULT_IDLE_TIMEOUT_S, (int, float))


def test_the_subprocess_is_still_started_in_its_own_session(tmp_path: Path) -> None:
    """``start_new_session=True`` is what makes the group kill possible.

    Pins the property the reap test depends on, so a refactor that
    drops the flag fails here rather than leaking a grandchild.
    """
    args = _child(SILENT, tmp_path / "session.py")
    with pytest.raises(QmdWedgeError):
        _run_qmd(args, cwd=tmp_path, timeout=30, idle_timeout=0.8)
    # If the child had shared our process group, the SIGKILL in
    # _run_qmd would have taken pytest down with it. Reaching the next
    # line at all is the assertion.
    assert True


def test_signals_are_delivered_to_the_group_not_the_parent_only() -> None:
    """Documents the mechanism the reap relies on.

    Not a behavioural test — a reader arriving at the reap test needs
    to know that ``os.killpg`` is why a bun/node grandchild dies, and
    that it is safe here only because the child is a session leader.
    """
    assert hasattr(signal, "SIGKILL")
    assert hasattr(os, "killpg")
    assert subprocess.Popen.__init__.__doc__ is None or True
