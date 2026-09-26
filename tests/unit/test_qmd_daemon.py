from __future__ import annotations

import subprocess
from pathlib import Path

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
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(
        args=["qmd"],
        returncode=returncode,
        stdout=stdout.encode("utf-8"),
        stderr=stderr.encode("utf-8"),
    )


def test_qmd_installed_false_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qmd_daemon.shutil, "which", lambda _name: None)
    assert qmd_daemon.qmd_installed() is False


def test_qmd_installed_true_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qmd_daemon.shutil, "which", lambda _name: "/usr/bin/qmd")
    assert qmd_daemon.qmd_installed() is True


def test_state_parses_running_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qmd_daemon.shutil, "which", lambda _name: "/usr/bin/qmd")
    monkeypatch.setattr(qmd_daemon, "_run_qmd", lambda *a, **k: _completed(_STATUS_RUNNING))
    state = qmd_daemon.qmd_daemon_state()
    assert state.installed is True
    assert state.running is True
    assert state.pid == 248654


def test_state_reports_stopped_without_mcp_line(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qmd_daemon.shutil, "which", lambda _name: "/usr/bin/qmd")
    monkeypatch.setattr(qmd_daemon, "_run_qmd", lambda *a, **k: _completed(_STATUS_STOPPED))
    state = qmd_daemon.qmd_daemon_state()
    assert state.running is False
    assert state.pid is None


def test_state_reports_nonzero_exit_and_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qmd_daemon.shutil, "which", lambda _name: "/usr/bin/qmd")
    monkeypatch.setattr(
        qmd_daemon,
        "_run_qmd",
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


def test_ensure_returns_running_on_clean_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(qmd_daemon.shutil, "which", lambda _name: "/usr/bin/qmd")
    calls: list[list[str]] = []

    def _run(args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(args)
        if args[1:] == ["mcp", "--http", "--daemon"]:
            return _completed("")
        return _completed(_STATUS_RUNNING)

    monkeypatch.setattr(qmd_daemon, "_run_qmd", _run)
    state = qmd_daemon.ensure_qmd_daemon(data_dir=tmp_path / "wiki")
    assert state.running is True
    assert ["qmd", "mcp", "--http", "--daemon"] in calls


def test_ensure_accepts_already_running_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """qmd's own idempotence path must not read as a failure."""
    monkeypatch.setattr(qmd_daemon.shutil, "which", lambda _name: "/usr/bin/qmd")

    def _run(args, **kwargs):  # type: ignore[no-untyped-def]
        if args[1:] == ["mcp", "--http", "--daemon"]:
            return _completed("Already running (PID 248654). Run 'qmd mcp stop' first.")
        return _completed(_STATUS_RUNNING)

    monkeypatch.setattr(qmd_daemon, "_run_qmd", _run)
    state = qmd_daemon.ensure_qmd_daemon(data_dir=tmp_path / "wiki")
    assert state.running is True


def test_ensure_is_non_fatal_on_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(qmd_daemon.shutil, "which", lambda _name: "/usr/bin/qmd")
    monkeypatch.setattr(qmd_daemon, "_run_qmd", lambda *a, **k: _completed("boom", returncode=1))
    state = qmd_daemon.ensure_qmd_daemon(data_dir=tmp_path / "wiki")
    assert state.running is False
    assert state.detail


def test_ensure_is_non_fatal_on_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(qmd_daemon.shutil, "which", lambda _name: "/usr/bin/qmd")

    def _raise(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd="qmd", timeout=15.0)

    monkeypatch.setattr(qmd_daemon, "_run_qmd", _raise)
    state = qmd_daemon.ensure_qmd_daemon(data_dir=tmp_path / "wiki")
    assert state.running is False
    assert "timed out" in state.detail


def test_ensure_is_non_fatal_when_qmd_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(qmd_daemon.shutil, "which", lambda _name: None)
    state = qmd_daemon.ensure_qmd_daemon(data_dir=tmp_path / "wiki")
    assert state.installed is False
    assert state.running is False


def test_module_exposes_no_stop_function() -> None:
    """qmd is machine-global and shared; nothing in LIES may stop it."""
    names = [n for n in dir(qmd_daemon) if "stop" in n.lower() or "kill" in n.lower()]
    assert names == []


def test_daemon_lifecycle_uses_run_qmd_not_subprocess_run() -> None:
    """Pin the PR #105 follow-up migration: the 3 daemon-lifecycle sites
    must not call ``subprocess.run(...)`` (the str-returning, pipe-buffer-
    deadlock-prone wrapper). They must route through ``_run_qmd`` instead.

    Plain string grep on the source. The forbidden patterns are exact:
    ``subprocess.run(`` is the str-returning call site shape, and the
    ``TODO(qmd-drain follow-up)`` markers explicitly pointed at the
    deferred migration. ``subprocess.TimeoutExpired`` stays legitimate
    (still imported and raised in the ``except`` branches).
    """
    from pathlib import Path as _Path

    src_path = _Path(qmd_daemon.__file__)
    text = src_path.read_text()

    # No ``subprocess.run(`` call sites should remain in daemon.py — every
    # one is a deadlock risk under a long qmd stderr trace.
    forbidden_subprocess_run = text.count("subprocess.run(")
    assert forbidden_subprocess_run == 0, (
        f"daemon.py still contains {forbidden_subprocess_run} "
        f"`subprocess.run(` call site(s); route them through `_run_qmd` "
        f"instead. See PR #105 follow-up (c85f80f)."
    )

    # The TODO markers explicitly referencing the deferred migration must
    # be gone — leaving them would lie about the file's state.
    forbidden_todo = text.count("TODO(qmd-drain follow-up)")
    assert forbidden_todo == 0, (
        f"daemon.py still contains {forbidden_todo} `TODO(qmd-drain "
        f"follow-up)` marker(s); strip them now that the migration is done."
    )

    # Sanity: ``subprocess.TimeoutExpired`` is still used in ``except``
    # branches, so the import must remain.
    assert "subprocess.TimeoutExpired" in text, (
        "daemon.py should still reference subprocess.TimeoutExpired "
        "(the except branches catch it from _run_qmd)."
    )
