from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lies.mcp import daemon
from lies.utils.lock_heartbeat import AcquireResult
from tests.conftest import make_wiki


def _record(**overrides: object) -> daemon.PidRecord:
    base: dict[str, object] = {
        "pid": os.getpid(),
        "host": "127.0.0.1",
        "port": 8737,
        "transport": "http",
        "started_at": datetime.now(UTC),
        "wiki_root": "/tmp/wiki",
        "version": "0.5.0",
    }
    base.update(overrides)
    return daemon.PidRecord(**base)  # type: ignore[arg-type]


def _wiki(tmp_path: Path):
    """Build a Wiki whose role-routed paths land under tmp_path."""
    return make_wiki(name="daemon-test", data_root=tmp_path)


def test_paths_live_under_runtime_and_state_roles(tmp_path: Path) -> None:
    wiki = _wiki(tmp_path)
    assert daemon.pid_path(wiki) == wiki.mcp_pid_path
    assert daemon.create_lock_path(wiki) == wiki.mcp_create_lock_path
    assert daemon.log_path(wiki) == wiki.mcp_log_path


def test_write_then_read_round_trips(tmp_path: Path) -> None:
    wiki = _wiki(tmp_path)
    rec = _record(wiki_root=str(wiki.data_root))
    daemon.write_record(wiki, rec)
    loaded = daemon.read_record(wiki)
    assert loaded is not None
    assert loaded.pid == rec.pid
    assert loaded.port == 8737
    assert loaded.transport == "http"


def test_read_missing_returns_none(tmp_path: Path) -> None:
    wiki = _wiki(tmp_path)
    assert daemon.read_record(wiki) is None


def test_read_corrupt_returns_none(tmp_path: Path) -> None:
    wiki = _wiki(tmp_path)
    path = daemon.pid_path(wiki)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"pid": 1, "port":', encoding="utf-8")
    assert daemon.read_record(wiki) is None


def test_read_wrong_schema_returns_none(tmp_path: Path) -> None:
    wiki = _wiki(tmp_path)
    path = daemon.pid_path(wiki)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"totally": "different"}', encoding="utf-8")
    assert daemon.read_record(wiki) is None


def test_write_leaves_no_temp_file(tmp_path: Path) -> None:
    wiki = _wiki(tmp_path)
    daemon.write_record(wiki, _record())
    leftovers = [
        p.name for p in wiki.mcp_pid_path.parent.iterdir() if p.name != wiki.mcp_pid_path.name
    ]
    assert leftovers == []


def test_write_removes_temp_file_when_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fail_replace(_src: Path, _dst: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(daemon.os, "replace", _fail_replace)

    wiki = _wiki(tmp_path)
    with pytest.raises(OSError, match="replace failed"):
        daemon.write_record(wiki, _record())

    tmp_file = wiki.mcp_pid_path.with_suffix(wiki.mcp_pid_path.suffix + ".tmp")
    assert not tmp_file.exists()


def test_clear_record_is_idempotent(tmp_path: Path) -> None:
    wiki = _wiki(tmp_path)
    daemon.write_record(wiki, _record())
    daemon.clear_record(wiki)
    daemon.clear_record(wiki)
    assert daemon.read_record(wiki) is None


def test_process_alive_for_self() -> None:
    assert daemon.process_alive(os.getpid()) is True


def test_process_alive_for_missing_pid() -> None:
    assert daemon.process_alive(999_999_999) is False


def test_is_stale_true_for_live_non_daemon_pid() -> None:
    assert daemon.is_stale(_record(pid=os.getpid())) is True


def test_is_stale_falls_back_to_liveness_when_proc_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(daemon, "_daemon_cmdline_matches", lambda _pid: None)
    assert daemon.is_stale(_record(pid=os.getpid())) is False


def test_is_stale_true_for_dead_pid() -> None:
    assert daemon.is_stale(_record(pid=999_999_999)) is True


def test_daemon_url_uses_mount_path() -> None:
    url = daemon.daemon_url(_record(host="127.0.0.1", port=9001))
    assert url == f"http://127.0.0.1:9001{daemon.MCP_PATH}"


def test_daemon_url_brackets_ipv6_loopback() -> None:
    url = daemon.daemon_url(_record(host="::1", port=9001))
    assert url == f"http://[::1]:9001{daemon.MCP_PATH}"


def test_already_running_carries_the_record() -> None:
    rec = _record()
    exc = daemon.DaemonAlreadyRunning("already up", record=rec)
    assert exc.record is rec
    assert isinstance(exc, daemon.DaemonError)


def test_port_free_true_for_unbound(tmp_path: Path) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
    assert daemon.port_free("127.0.0.1", free_port) is True


def test_port_free_false_when_bound() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        port = held.getsockname()[1]
        assert daemon.port_free("127.0.0.1", port) is False


def test_tail_log_returns_last_lines(tmp_path: Path) -> None:
    wiki = _wiki(tmp_path)
    log = daemon.log_path(wiki)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("\n".join(f"line {i}" for i in range(50)), encoding="utf-8")
    assert daemon.tail_log(wiki, 3) == ["line 47", "line 48", "line 49"]


def test_tail_log_missing_file_is_empty(tmp_path: Path) -> None:
    wiki = _wiki(tmp_path)
    assert daemon.tail_log(wiki, 5) == []


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "127.0.0.2"])
def test_loopback_hosts_are_accepted(host: str) -> None:
    daemon.require_loopback_host(host)


def test_spawn_rejects_non_loopback_before_acquiring_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _unexpected_lock(*_args: object, **_kwargs: object) -> None:
        pytest.fail("spawn_daemon acquired the create lock for a rejected host")

    monkeypatch.setattr(daemon, "acquire_create_lock", _unexpected_lock)

    wiki = _wiki(tmp_path)
    with pytest.raises(daemon.NonLoopbackBind, match="0.0.0.0"):
        daemon.spawn_daemon(wiki, host="0.0.0.0")

    # XDG: spawning with a non-loopback host must not materialise any
    # runtime/state directories on disk.
    assert not wiki.runtime_root.exists()
    assert not wiki.state_root.exists()


def test_spawn_raises_when_port_occupied(tmp_path: Path) -> None:
    wiki = _wiki(tmp_path)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        port = held.getsockname()[1]
        with pytest.raises(daemon.PortUnavailable):
            daemon.spawn_daemon(wiki, host="127.0.0.1", port=port, timeout=1.0)


def test_spawn_raises_already_running_for_live_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wiki = _wiki(tmp_path)
    monkeypatch.setattr(daemon, "_daemon_cmdline_matches", lambda _pid: True)
    daemon.write_record(wiki, _record(pid=os.getpid()))
    with pytest.raises(daemon.DaemonAlreadyRunning) as caught:
        daemon.spawn_daemon(wiki, timeout=1.0)
    assert caught.value.record.pid == os.getpid()


def test_spawn_reclaims_stale_record_then_fails_on_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale record is cleared, not treated as a live daemon."""
    wiki = _wiki(tmp_path)
    daemon.write_record(wiki, _record(pid=999_999_999))

    # Capture the real Popen before monkeypatching; the helper below
    # would otherwise recurse into itself because daemon.subprocess is
    # the same module object as subprocess and the patch is global.
    real_popen = subprocess.Popen

    def _instant_exit(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        return real_popen([sys.executable, "-c", "raise SystemExit(3)"], **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(daemon.subprocess, "Popen", _instant_exit)
    with pytest.raises(daemon.DaemonStartFailed):
        daemon.spawn_daemon(wiki, port=8739, timeout=3.0)
    assert daemon.read_record(wiki) is None


def test_spawn_raises_busy_when_create_lock_held(tmp_path: Path) -> None:
    from lies.utils.exclusive import acquire_create_lock, release_create_lock

    wiki = _wiki(tmp_path)
    lock = daemon.create_lock_path(wiki)
    result = acquire_create_lock(lock, max_age_s=daemon.CREATE_LOCK_MAX_AGE_S)
    assert result is not None
    try:
        with pytest.raises(daemon.DaemonBusy):
            daemon.spawn_daemon(wiki, timeout=1.0)
    finally:
        release_create_lock(lock, result.fd)


def test_stop_with_no_record_is_a_noop(tmp_path: Path) -> None:
    wiki = _wiki(tmp_path)
    result = daemon.stop_daemon(wiki)
    assert result.action == "none"
    assert result.pid is None


def test_stop_clears_a_stale_record(tmp_path: Path) -> None:
    wiki = _wiki(tmp_path)
    daemon.write_record(wiki, _record(pid=999_999_999))
    result = daemon.stop_daemon(wiki)
    assert result.action == "cleared_stale"
    assert result.pid == 999_999_999
    assert daemon.read_record(wiki) is None


def test_stop_terminates_a_cooperative_child(tmp_path: Path) -> None:
    wiki = _wiki(tmp_path)
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)", "lies.cli", "_serve"]
    )
    daemon.write_record(wiki, _record(pid=proc.pid))
    try:
        result = daemon.stop_daemon(wiki, grace=5.0)
        assert result.action == "stopped"
        assert result.signal == "SIGTERM"
        assert daemon.read_record(wiki) is None
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_stop_escalates_to_sigkill(tmp_path: Path) -> None:
    """A child that ignores SIGTERM is killed after the grace period."""
    wiki = _wiki(tmp_path)
    ready_marker = wiki.runtime_root / "handler_ready"
    ready_marker.parent.mkdir(parents=True, exist_ok=True)
    script = (
        "import signal, sys, time;"
        f"signal.signal(signal.SIGTERM, signal.SIG_IGN);"
        f"open({str(ready_marker)!r}, 'w').close();"
        "time.sleep(60)"
    )
    proc = subprocess.Popen([sys.executable, "-c", script, "lies.cli", "_serve"])
    # Block until the child has installed the SIG_IGN handler. Without
    # this handshake the parent's SIGTERM can land first and the test
    # races — the child would exit from the default SIGTERM action
    # before its handler is in place.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if ready_marker.exists():
            break
        time.sleep(0.01)
    else:
        proc.kill()
        proc.wait(timeout=5)
        pytest.fail("child never installed its SIGTERM handler within 5s")
    daemon.write_record(wiki, _record(pid=proc.pid))
    try:
        result = daemon.stop_daemon(wiki, grace=1.0)
        assert result.action == "stopped"
        assert result.signal == "SIGKILL"
        assert daemon.read_record(wiki) is None
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


def test_stop_raises_busy_when_create_lock_held(tmp_path: Path) -> None:
    from lies.utils.exclusive import acquire_create_lock, release_create_lock

    wiki = _wiki(tmp_path)
    lock = daemon.create_lock_path(wiki)
    result = acquire_create_lock(lock, max_age_s=daemon.CREATE_LOCK_MAX_AGE_S)
    assert result is not None
    try:
        with pytest.raises(daemon.DaemonBusy):
            daemon.stop_daemon(wiki)
    finally:
        release_create_lock(lock, result.fd)


def test_stop_exits_with_code_5_on_indeterminate_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An indeterminate create-lock result exits with code 5 and an operator message."""
    wiki = _wiki(tmp_path)
    indeterminate = AcquireResult(
        fd=-1,
        holder_pid=999,
        holder_started_at=1723828800.0,
        status="indeterminate",
    )
    monkeypatch.setattr(daemon, "acquire_create_lock", lambda *a, **k: indeterminate)
    with pytest.raises(SystemExit) as caught:
        daemon.stop_daemon(wiki)
    assert caught.value.code == 5
    err = capsys.readouterr().err
    assert "pid 999" in err
    assert "force-repair" in err


def test_status_reports_stopped_without_a_record(tmp_path: Path) -> None:
    wiki = _wiki(tmp_path)
    status = daemon.daemon_status(wiki)
    assert status.running is False
    assert status.stale is False
    assert status.record is None
    assert status.url is None
    assert status.uptime_s is None
    assert status.log == daemon.log_path(wiki)


def test_status_reports_running_for_a_live_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wiki = _wiki(tmp_path)
    monkeypatch.setattr(daemon, "_daemon_cmdline_matches", lambda _pid: True)
    daemon.write_record(wiki, _record(pid=os.getpid(), port=9002))
    status = daemon.daemon_status(wiki)
    assert status.running is True
    assert status.stale is False
    assert status.url == f"http://127.0.0.1:9002{daemon.MCP_PATH}"
    assert status.uptime_s is not None
    assert status.uptime_s >= 0


def test_status_reports_stale_for_a_dead_pid(tmp_path: Path) -> None:
    wiki = _wiki(tmp_path)
    daemon.write_record(wiki, _record(pid=999_999_999))
    status = daemon.daemon_status(wiki)
    assert status.running is False
    assert status.stale is True
    assert status.record is not None
    # Read-only: status reports a stale record as stale and does NOT
    # clear it; only ``down`` may mutate the pidfile.
    assert daemon.read_record(wiki) is not None


def test_status_clamps_negative_uptime_to_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record with a future ``started_at`` reports uptime >= 0, not negative."""
    wiki = _wiki(tmp_path)
    monkeypatch.setattr(daemon, "_daemon_cmdline_matches", lambda _pid: True)
    future = datetime.now(UTC) + timedelta(hours=1)
    daemon.write_record(wiki, _record(pid=os.getpid(), started_at=future))
    status = daemon.daemon_status(wiki)
    assert status.running is True
    assert status.uptime_s is not None
    assert status.uptime_s == 0.0


def test_spawn_exits_with_code_5_on_indeterminate_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An indeterminate create-lock result exits with code 5 and an operator message."""
    wiki = _wiki(tmp_path)
    indeterminate = AcquireResult(
        fd=-1,
        holder_pid=999,
        holder_started_at=1723828800.0,
        status="indeterminate",
    )
    monkeypatch.setattr(daemon, "acquire_create_lock", lambda *a, **k: indeterminate)
    with pytest.raises(SystemExit) as caught:
        daemon.spawn_daemon(wiki, timeout=1.0)
    assert caught.value.code == 5
    err = capsys.readouterr().err
    assert "pid 999" in err
    assert "force-repair" in err
