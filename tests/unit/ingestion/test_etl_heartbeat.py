import os
import time
from pathlib import Path

from lies.etl.heartbeat import (
    MAX_SYNC_AGE_S,
    Heartbeat,
    clear_heartbeat,
    heartbeat_is_stale,
    pid_alive,
    read_heartbeat,
    wait_until_free,  # noqa: F401
    write_heartbeat,
)


def test_write_and_read_heartbeat(tmp_path: Path) -> None:
    h = Heartbeat(pid=os.getpid(), started_at=time.time(), collection="cpython")
    write_heartbeat(tmp_path, h)
    assert read_heartbeat(tmp_path) is not None


def test_pid_alive_for_self() -> None:
    assert pid_alive(os.getpid()) is True


def test_pid_alive_for_missing() -> None:
    assert pid_alive(999_999_999) is False


def test_clear_heartbeat(tmp_path: Path) -> None:
    h = Heartbeat(pid=os.getpid(), started_at=time.time(), collection="x")
    write_heartbeat(tmp_path, h)
    clear_heartbeat(tmp_path)
    assert read_heartbeat(tmp_path) is None


def test_stale_when_old(tmp_path: Path) -> None:
    h = Heartbeat(pid=os.getpid(), started_at=time.time() - MAX_SYNC_AGE_S - 10, collection="x")
    assert heartbeat_is_stale(h) is True
