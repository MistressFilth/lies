from __future__ import annotations

import os
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from lies.mcp import daemon


def _record(**overrides: object) -> daemon.PidRecord:
    base: dict[str, object] = {
        "pid": os.getpid(),
        "host": "127.0.0.1",
        "port": 8737,
        "transport": "http",
        "started_at": datetime.now(timezone.utc),
        "wiki_root": "/tmp/wiki",
        "version": "0.5.0",
    }
    base.update(overrides)
    return daemon.PidRecord(**base)  # type: ignore[arg-type]


def test_paths_live_under_dot_lies(tmp_path: Path) -> None:
    assert daemon.pid_path(tmp_path) == tmp_path / ".lies" / "mcp.pid"
    assert daemon.create_lock_path(tmp_path) == tmp_path / ".lies" / "mcp.pid.create"
    assert daemon.log_path(tmp_path) == tmp_path / ".lies" / "mcp.log"


def test_write_then_read_round_trips(tmp_path: Path) -> None:
    rec = _record()
    daemon.write_record(tmp_path, rec)
    loaded = daemon.read_record(tmp_path)
    assert loaded is not None
    assert loaded.pid == rec.pid
    assert loaded.port == 8737
    assert loaded.transport == "http"


def test_read_missing_returns_none(tmp_path: Path) -> None:
    assert daemon.read_record(tmp_path) is None


def test_read_corrupt_returns_none(tmp_path: Path) -> None:
    path = daemon.pid_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"pid": 1, "port":', encoding="utf-8")
    assert daemon.read_record(tmp_path) is None


def test_read_wrong_schema_returns_none(tmp_path: Path) -> None:
    path = daemon.pid_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"totally": "different"}', encoding="utf-8")
    assert daemon.read_record(tmp_path) is None


def test_write_leaves_no_temp_file(tmp_path: Path) -> None:
    daemon.write_record(tmp_path, _record())
    leftovers = [p.name for p in (tmp_path / ".lies").iterdir() if p.name != "mcp.pid"]
    assert leftovers == []


def test_clear_record_is_idempotent(tmp_path: Path) -> None:
    daemon.write_record(tmp_path, _record())
    daemon.clear_record(tmp_path)
    daemon.clear_record(tmp_path)
    assert daemon.read_record(tmp_path) is None


def test_process_alive_for_self() -> None:
    assert daemon.process_alive(os.getpid()) is True


def test_process_alive_for_missing_pid() -> None:
    assert daemon.process_alive(999_999_999) is False


def test_is_stale_false_for_live_pid() -> None:
    assert daemon.is_stale(_record(pid=os.getpid())) is False


def test_is_stale_true_for_dead_pid() -> None:
    assert daemon.is_stale(_record(pid=999_999_999)) is True


def test_daemon_url_uses_mount_path() -> None:
    url = daemon.daemon_url(_record(host="127.0.0.1", port=9001))
    assert url == f"http://127.0.0.1:9001{daemon.MCP_PATH}"


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


def test_ensure_daemon_gitignored_lists_all_three(tmp_path: Path) -> None:
    daemon.ensure_daemon_gitignored(tmp_path)
    body = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert ".lies/mcp.pid\n" in body
    assert ".lies/mcp.pid.create\n" in body
    assert ".lies/mcp.log\n" in body


def test_tail_log_returns_last_lines(tmp_path: Path) -> None:
    log = daemon.log_path(tmp_path)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("\n".join(f"line {i}" for i in range(50)), encoding="utf-8")
    assert daemon.tail_log(tmp_path, 3) == ["line 47", "line 48", "line 49"]


def test_tail_log_missing_file_is_empty(tmp_path: Path) -> None:
    assert daemon.tail_log(tmp_path, 5) == []


def test_spawn_raises_when_port_occupied(tmp_path: Path) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        port = held.getsockname()[1]
        with pytest.raises(daemon.PortUnavailable):
            daemon.spawn_daemon(tmp_path, host="127.0.0.1", port=port, timeout=1.0)


def test_spawn_raises_already_running_for_live_record(tmp_path: Path) -> None:
    daemon.write_record(tmp_path, _record(pid=os.getpid()))
    with pytest.raises(daemon.DaemonAlreadyRunning) as caught:
        daemon.spawn_daemon(tmp_path, timeout=1.0)
    assert caught.value.record.pid == os.getpid()


def test_spawn_reclaims_stale_record_then_fails_on_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale record is cleared, not treated as a live daemon."""
    daemon.write_record(tmp_path, _record(pid=999_999_999))

    # Capture the real Popen before monkeypatching; the helper below
    # would otherwise recurse into itself because daemon.subprocess is
    # the same module object as subprocess and the patch is global.
    real_popen = subprocess.Popen

    def _instant_exit(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        return real_popen([sys.executable, "-c", "raise SystemExit(3)"], **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(daemon.subprocess, "Popen", _instant_exit)
    with pytest.raises(daemon.DaemonStartFailed):
        daemon.spawn_daemon(tmp_path, port=8739, timeout=3.0)
    assert daemon.read_record(tmp_path) is None


def test_spawn_raises_busy_when_create_lock_held(tmp_path: Path) -> None:
    from lies.utils.exclusive import acquire_create_lock, release_create_lock

    lock = daemon.create_lock_path(tmp_path)
    fd = acquire_create_lock(lock, max_age_s=daemon.CREATE_LOCK_MAX_AGE_S)
    try:
        with pytest.raises(daemon.DaemonBusy):
            daemon.spawn_daemon(tmp_path, timeout=1.0)
    finally:
        release_create_lock(lock, fd)
