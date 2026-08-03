from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

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
