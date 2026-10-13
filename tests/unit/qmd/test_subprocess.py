"""Tests for the deadlock-free subprocess helper."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from lies.qmd._subprocess import QmdWedgeError, _run_qmd


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
    """A subprocess that ignores SIGTERM gets SIGKILL'd on timeout."""
    import signal

    script = tmp_path / "zombie.py"
    script.write_text(
        "import signal, time, os\n"
        f"signal.signal({signal.SIGTERM}, signal.SIG_IGN)\n"
        "time.sleep(60)\n"
    )
    with pytest.raises(subprocess.TimeoutExpired):
        _run_qmd(
            [sys.executable, str(script)],
            cwd=tmp_path,
            timeout=1.0,
        )


def test_run_qmd_does_not_deadlock_on_long_stderr(tmp_path: Path):
    """A subprocess writing 100 KB to stderr must not hang the parent."""
    script = tmp_path / "loud.py"
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
    """Timeout SIGKILLs the entire process group, not just the immediate child."""
    script = tmp_path / "spawn_grandchild.py"
    script.write_text(
        "import os, signal, time\n"
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
    """Long-stderr path also kills the entire process group on timeout."""
    script = tmp_path / "loud_grandchild.py"
    script.write_text(
        "import os, signal, sys, time\n"
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


# `_child_env` forces NO_COLOR=1 on every qmd child. Verified against
# qmd 2.5.3: its only NO_COLOR consumer is
# `dist/cli/qmd.js:92`, `useColor = !NO_COLOR && process.stdout.isTTY`,
# and LIES always pipes, so colour is already off and the override
# cannot change today's bytes. The `⠋ Gathering information` spinner
# that motivated an earlier form of these tests is emitted by `ipull`
# during a *model download*, not gated by NO_COLOR — the fix is a warm
# model cache, not an env var.


def test_child_env_forces_no_color(monkeypatch: pytest.MonkeyPatch) -> None:
    """``NO_COLOR`` is set on every qmd child."""
    from lies.qmd._subprocess import _child_env

    monkeypatch.delenv("NO_COLOR", raising=False)
    assert _child_env()["NO_COLOR"] == "1"


def test_child_env_overrides_an_operator_who_re_enabled_color(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exported ``NO_COLOR=0`` must not reach a stream LIES parses."""
    from lies.qmd._subprocess import _child_env

    monkeypatch.setenv("NO_COLOR", "0")
    assert _child_env()["NO_COLOR"] == "1"


def test_child_env_inherits_the_rest_of_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only ``NO_COLOR`` is overridden; nothing else is dropped."""
    from lies.qmd._subprocess import _child_env

    monkeypatch.setenv("QMD_INDEX", "/tmp/some-index.sqlite")
    monkeypatch.setenv("QMD_EMBED_PARALLELISM", "1")
    env = _child_env()
    assert env["QMD_INDEX"] == "/tmp/some-index.sqlite"
    assert env["QMD_EMBED_PARALLELISM"] == "1"


def test_run_qmd_passes_the_child_env_to_the_process(tmp_path: Path) -> None:
    """The override is actually applied, not merely computed."""
    import sys

    script = tmp_path / "show_env.py"
    script.write_text("import os, sys\nsys.stdout.write(os.environ.get('NO_COLOR', '<unset>'))\n")

    result = _run_qmd([sys.executable, str(script)], cwd=tmp_path, timeout=10.0)

    assert result.stdout.decode().strip() == "1", (
        f"child did not receive NO_COLOR=1; got {result.stdout.decode()!r}"
    )


@pytest.mark.slow
def test_idle_bound_fires_before_the_total_bound(tmp_path: Path) -> None:
    """A silent child is reported ``idle`` with its output tail — not ``total``."""
    script = tmp_path / "silent.py"
    script.write_text(
        "import sys, time\nsys.stderr.write('phase 1\\n'); sys.stderr.flush()\ntime.sleep(60)\n"
    )

    with pytest.raises(QmdWedgeError) as excinfo:
        _run_qmd(
            [sys.executable, str(script)],
            cwd=tmp_path,
            timeout=30.0,
            idle_timeout=1.0,
        )

    assert excinfo.value.bound == "idle", (
        f"expected an idle wedge, got bound={excinfo.value.bound!r}"
    )
    assert "phase 1" in excinfo.value.last_output, (
        f"the output tail was lost; last_output={excinfo.value.last_output!r}"
    )


def test_silent_command_idle_bound_stays_below_the_total() -> None:
    """The fraction is strictly below 1.0, or the idle bound is dead code."""
    from lies.qmd._subprocess import SILENT_COMMAND_IDLE_TIMEOUT_FRACTION

    assert 0 < SILENT_COMMAND_IDLE_TIMEOUT_FRACTION < 1, (
        "the idle bound must stay below the total, or the total bound is "
        "always checked first and the idle branch never fires"
    )


def test_a_wedge_stderr_is_capped_like_the_success_path(tmp_path: Path):
    """Both wedge raises carry the same ``_MAX_STDERR_BYTES`` buffer."""
    from lies.qmd._subprocess import _MAX_STDERR_BYTES

    script = tmp_path / "loud_then_hang.py"
    script.write_text(
        "import sys, time\nsys.stderr.write('x' * 100_000)\nsys.stderr.flush()\ntime.sleep(30)\n"
    )

    with pytest.raises(QmdWedgeError) as excinfo:
        _run_qmd(
            [sys.executable, str(script)],
            cwd=tmp_path,
            timeout=0.12,
            idle_timeout=0.08,
        )

    captured = excinfo.value.stderr or b""
    assert len(captured) <= _MAX_STDERR_BYTES, (
        f"a wedge handed back {len(captured)} bytes of stderr; the cap is {_MAX_STDERR_BYTES}"
    )
