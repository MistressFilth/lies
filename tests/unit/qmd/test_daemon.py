from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from lies.qmd import daemon as qmd_daemon


def test_sidecar_read_write_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qmd_daemon, "SIDECAR_PATH", tmp_path / "mcp.data-dir")
    qmd_daemon.write_sidecar_data_dir(tmp_path / "expected")
    assert qmd_daemon.read_sidecar_data_dir() == tmp_path / "expected"


def test_check_data_dir_match_returns_true_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(qmd_daemon, "SIDECAR_PATH", tmp_path / "mcp.data-dir")
    # Sidecar absent — first run, no mismatch.
    assert qmd_daemon.check_data_dir_match(tmp_path / "expected") is True


def test_check_data_dir_match_returns_false_on_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(qmd_daemon, "SIDECAR_PATH", tmp_path / "mcp.data-dir")
    qmd_daemon.write_sidecar_data_dir(tmp_path / "actual")
    assert qmd_daemon.check_data_dir_match(tmp_path / "expected") is False


def test_check_data_dir_match_normalizes_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``Path("wiki")`` and ``Path("./wiki")`` must compare equal after
    ``Path.resolve``; literal equality would spuriously report a mismatch
    and force a needless reap when callers pass equivalent-but-not-
    identical path objects.
    """
    monkeypatch.setattr(qmd_daemon, "SIDECAR_PATH", tmp_path / "mcp.data-dir")
    qmd_daemon.write_sidecar_data_dir(tmp_path / "wiki")
    # `tmp_path / "." / "wiki"` resolves to the same path as `tmp_path / "wiki"`.
    dotted = Path(str(tmp_path)) / "." / "wiki"
    assert qmd_daemon.check_data_dir_match(dotted) is True


def test_ensure_qmd_daemon_reaps_on_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(qmd_daemon, "SIDECAR_PATH", tmp_path / "mcp.data-dir")
    qmd_daemon.write_sidecar_data_dir(tmp_path / "stale")
    # Mock qmd subprocess calls so the test does not require qmd.
    reap_called: list[bool] = []
    spawn_called: list[bool] = []
    monkeypatch.setattr(qmd_daemon, "_reap_qmd_daemon", lambda: reap_called.append(True))
    monkeypatch.setattr(qmd_daemon, "_spawn_qmd_daemon", lambda: spawn_called.append(True))
    monkeypatch.setattr(qmd_daemon, "qmd_installed", lambda: True)
    qmd_daemon.ensure_qmd_daemon(data_dir=tmp_path / "fresh")
    assert reap_called == [True]
    assert spawn_called == [True]
    assert qmd_daemon.read_sidecar_data_dir() == tmp_path / "fresh"


def test_reap_qmd_daemon_sends_sigterm_and_waits_for_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_reap_qmd_daemon`` must SIGTERM the daemon and not return until
    the process is actually gone. Regression test for the post-SIGTERM
    race where ``_reap_qmd_daemon`` returned before the daemon released
    its port, allowing the subsequent spawn to inherit the old index.
    """
    sleeper = tmp_path / "sleeper.py"
    sleeper.write_text("import time; time.sleep(30)\n")
    proc = subprocess.Popen([sys.executable, str(sleeper)])
    try:
        monkeypatch.setattr(
            qmd_daemon,
            "qmd_daemon_state",
            lambda: qmd_daemon.QmdState(True, True, proc.pid, f"sleeper {proc.pid}"),
        )
        start = time.monotonic()
        qmd_daemon._reap_qmd_daemon(grace=2.0, poll=0.02)
        elapsed = time.monotonic() - start
        # SIGTERM kills the sleeper quickly; elapsed must be well under
        # the 30s sleep, AND the process must actually be gone.
        assert elapsed < 2.0
        assert proc.poll() is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)


def test_reap_qmd_daemon_escalates_to_sigkill_when_sigterm_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A daemon that ignores SIGTERM must be killed via SIGKILL after
    the grace window. Pins the SIGKILL fallback in ``_reap_qmd_daemon``.
    """
    stubborn = tmp_path / "stubborn.py"
    stubborn.write_text(
        "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(30)\n"
    )
    proc = subprocess.Popen([sys.executable, str(stubborn)])
    # Give the interpreter time to install the SIG_IGN handler before reap
    # runs; otherwise the signal can arrive during Python startup and the
    # process dies on the default handler, defeating the test's intent.
    time.sleep(0.3)
    try:
        monkeypatch.setattr(
            qmd_daemon,
            "qmd_daemon_state",
            lambda: qmd_daemon.QmdState(True, True, proc.pid, f"stubborn {proc.pid}"),
        )
        start = time.monotonic()
        qmd_daemon._reap_qmd_daemon(grace=0.5, poll=0.02)
        elapsed = time.monotonic() - start
        # SIGTERM is ignored, so reap waits the grace window, then SIGKILLs.
        # Total elapsed must exceed the SIGTERM grace but stay well under 30s.
        assert 0.5 <= elapsed < 5.0
        assert proc.poll() is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)


def test_ensure_qmd_daemon_runs_real_spawn_after_mocked_reap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mismatch path: reap is mocked (the wait-helper tests above cover
    the real reap), but spawn invokes an actual subprocess via a stubbed
    qmd binary. The sidecar is only updated after spawn returns, so the
    new daemon — the one spawn just started — is the one whose data-dir
    is recorded. Regression test for the post-SIGTERM race window.
    """
    monkeypatch.setattr(qmd_daemon, "SIDECAR_PATH", tmp_path / "mcp.data-dir")
    qmd_daemon.write_sidecar_data_dir(tmp_path / "stale")

    # Stub qmd binary: `status` reports a running PID, `mcp` exits 0.
    script = tmp_path / "fake_qmd.py"
    script.write_text(
        "import sys\n"
        "if 'status' in sys.argv:\n"
        "    print('QMD Status')\n"
        "    print('Index: /tmp/index.sqlite')\n"
        "    print('MCP:   running (PID 99999)')\n"
        "    print()\n"
        "    print('Documents')\n"
        "    print('  Total:    0 files indexed')\n"
        "elif 'mcp' in sys.argv:\n"
        "    pass\n"
    )

    # Route every subprocess call that targets the qmd binary through
    # our stub.
    monkeypatch.setattr(qmd_daemon, "_QMD_BIN", sys.executable)
    monkeypatch.setattr(qmd_daemon.shutil, "which", lambda _n: sys.executable)
    real_run = qmd_daemon.subprocess.run

    def _route(args, **kwargs):  # type: ignore[no-untyped-def]
        if args and args[0] == sys.executable:
            return real_run([sys.executable, str(script), *args[1:]], **kwargs)
        return real_run(args, **kwargs)

    monkeypatch.setattr(qmd_daemon.subprocess, "run", _route)

    reap_calls: list[bool] = []
    monkeypatch.setattr(qmd_daemon, "_reap_qmd_daemon", lambda: reap_calls.append(True))

    qmd_daemon.ensure_qmd_daemon(data_dir=tmp_path / "fresh")

    assert reap_calls == [True]
    # Sidecar recorded the new data-dir only after spawn returned.
    assert qmd_daemon.read_sidecar_data_dir() == tmp_path / "fresh"
