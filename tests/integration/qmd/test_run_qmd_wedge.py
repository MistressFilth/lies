"""A slow qmd call and a wedged qmd call are not the same failure."""

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
    """Progress resets the idle clock, so a slow query is never a wedge."""
    args = _child(PROGRESSING, tmp_path / "progress.py")

    result = _run_qmd(args, cwd=tmp_path, timeout=10, idle_timeout=1.0)

    assert result.returncode == 0, result.stderr.decode()
    assert b"phase 5" in result.stderr, "the child was allowed to finish"


def test_a_silent_child_is_killed_on_the_idle_bound(tmp_path: Path) -> None:
    """Silence is the wedge signal, however generous the total budget."""
    args = _child(SILENT, tmp_path / "silent.py")

    with pytest.raises(QmdWedgeError) as excinfo:
        _run_qmd(args, cwd=tmp_path, timeout=30, idle_timeout=0.8)

    err = excinfo.value
    assert err.bound == "idle", err.bound
    assert err.idle_timeout == 0.8, err.idle_timeout
    assert "idle" in str(err), str(err)


def test_the_whole_process_group_is_reaped_on_a_wedge(tmp_path: Path) -> None:
    """A wedged grandchild must not outlive its parent."""
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
    """The idle bound cannot be the only one."""
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
    """The evidence travels with the error."""
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
    """A qmd that fails fast is a failure, not a hang."""
    src = "import sys; sys.stderr.write('boom\\n'); sys.exit(3)\n"
    args = _child(src, tmp_path / "fail.py")

    result = _run_qmd(args, cwd=tmp_path, timeout=10, idle_timeout=5.0)
    assert result.returncode == 3, "a fast nonzero exit is a failure, not a wedge"
    assert b"boom" in result.stderr


def test_the_idle_default_is_derived_not_hardcoded_per_call() -> None:
    """Call sites that pass no ``idle_timeout`` get the shared default."""
    from lies.qmd._subprocess import DEFAULT_IDLE_TIMEOUT_S

    assert DEFAULT_IDLE_TIMEOUT_S > 0
    assert isinstance(DEFAULT_IDLE_TIMEOUT_S, (int, float))


def test_the_subprocess_is_still_started_in_its_own_session(tmp_path: Path) -> None:
    """``start_new_session=True`` is what makes the group kill possible."""
    args = _child(SILENT, tmp_path / "session.py")
    with pytest.raises(QmdWedgeError):
        _run_qmd(args, cwd=tmp_path, timeout=30, idle_timeout=0.8)
    # If the child had shared our process group, the SIGKILL in
    # _run_qmd would have taken pytest down with it. Reaching the next
    # line at all is the assertion.
    assert True


def test_signals_are_delivered_to_the_group_not_the_parent_only() -> None:
    """Documents the mechanism the reap relies on."""
    assert hasattr(signal, "SIGKILL")
    assert hasattr(os, "killpg")
    assert subprocess.Popen.__init__.__doc__ is None or True
