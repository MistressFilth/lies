from __future__ import annotations

import subprocess

import pytest

from lies.qmd import daemon as qmd_daemon

_STATUS_RUNNING = """QMD Status

Index: /home/u/.cache/qmd/index.sqlite
Size:  4.0 KB
MCP:   running (PID 248654)

Documents
  Total:    0 files indexed
"""

_STATUS_STOPPED = """QMD Status

Index: /home/u/.cache/qmd/index.sqlite
Size:  4.0 KB

Documents
  Total:    0 files indexed
"""


def _completed(
    stdout: str = "", returncode: int = 0, stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["qmd"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def test_qmd_installed_false_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qmd_daemon.shutil, "which", lambda _name: None)
    assert qmd_daemon.qmd_installed() is False


def test_qmd_installed_true_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qmd_daemon.shutil, "which", lambda _name: "/usr/bin/qmd")
    assert qmd_daemon.qmd_installed() is True


def test_state_parses_running_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qmd_daemon.shutil, "which", lambda _name: "/usr/bin/qmd")
    monkeypatch.setattr(qmd_daemon.subprocess, "run", lambda *a, **k: _completed(_STATUS_RUNNING))
    state = qmd_daemon.qmd_daemon_state()
    assert state.installed is True
    assert state.running is True
    assert state.pid == 248654


def test_state_reports_stopped_without_mcp_line(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qmd_daemon.shutil, "which", lambda _name: "/usr/bin/qmd")
    monkeypatch.setattr(qmd_daemon.subprocess, "run", lambda *a, **k: _completed(_STATUS_STOPPED))
    state = qmd_daemon.qmd_daemon_state()
    assert state.running is False
    assert state.pid is None


def test_state_reports_nonzero_exit_and_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qmd_daemon.shutil, "which", lambda _name: "/usr/bin/qmd")
    monkeypatch.setattr(
        qmd_daemon.subprocess,
        "run",
        lambda *a, **k: _completed(
            _STATUS_STOPPED, returncode=3, stderr="database is locked\nretry later\n"
        ),
    )

    state = qmd_daemon.qmd_daemon_state()

    assert state.running is False
    assert state.detail == "qmd status exited 3: database is locked"


def test_state_when_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qmd_daemon.shutil, "which", lambda _name: None)
    state = qmd_daemon.qmd_daemon_state()
    assert state.installed is False
    assert state.running is False
    assert "not installed" in state.detail


def test_ensure_returns_running_on_clean_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qmd_daemon.shutil, "which", lambda _name: "/usr/bin/qmd")
    calls: list[list[str]] = []

    def _run(args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(args)
        if args[1:] == ["mcp", "--http", "--daemon"]:
            return _completed("")
        return _completed(_STATUS_RUNNING)

    monkeypatch.setattr(qmd_daemon.subprocess, "run", _run)
    state = qmd_daemon.ensure_qmd_daemon()
    assert state.running is True
    assert ["qmd", "mcp", "--http", "--daemon"] in calls


def test_ensure_accepts_already_running_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """qmd's own idempotence path must not read as a failure."""
    monkeypatch.setattr(qmd_daemon.shutil, "which", lambda _name: "/usr/bin/qmd")

    def _run(args, **kwargs):  # type: ignore[no-untyped-def]
        if args[1:] == ["mcp", "--http", "--daemon"]:
            return _completed("Already running (PID 248654). Run 'qmd mcp stop' first.")
        return _completed(_STATUS_RUNNING)

    monkeypatch.setattr(qmd_daemon.subprocess, "run", _run)
    state = qmd_daemon.ensure_qmd_daemon()
    assert state.running is True


def test_ensure_is_non_fatal_on_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qmd_daemon.shutil, "which", lambda _name: "/usr/bin/qmd")
    monkeypatch.setattr(
        qmd_daemon.subprocess, "run", lambda *a, **k: _completed("boom", returncode=1)
    )
    state = qmd_daemon.ensure_qmd_daemon()
    assert state.running is False
    assert state.detail


def test_ensure_is_non_fatal_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qmd_daemon.shutil, "which", lambda _name: "/usr/bin/qmd")

    def _raise(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd="qmd", timeout=15.0)

    monkeypatch.setattr(qmd_daemon.subprocess, "run", _raise)
    state = qmd_daemon.ensure_qmd_daemon()
    assert state.running is False
    assert "timed out" in state.detail


def test_ensure_is_non_fatal_when_qmd_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qmd_daemon.shutil, "which", lambda _name: None)
    state = qmd_daemon.ensure_qmd_daemon()
    assert state.installed is False
    assert state.running is False


def test_module_exposes_no_stop_function() -> None:
    """qmd is machine-global and shared; nothing in LIES may stop it."""
    names = [n for n in dir(qmd_daemon) if "stop" in n.lower() or "kill" in n.lower()]
    assert names == []
