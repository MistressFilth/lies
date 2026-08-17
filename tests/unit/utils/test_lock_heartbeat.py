"""Tests for lock heartbeat helpers."""

from __future__ import annotations

from pathlib import Path

from lies.utils.lock_heartbeat import (
    Heartbeat,
    read_heartbeat,
    read_owner_pid,
    write_heartbeat,
    write_owner_pid,
)


def test_write_owner_pid_roundtrip(tmp_path: Path) -> None:
    pid_path = tmp_path / "memory.pid"
    write_owner_pid(pid_path, 12345)
    assert read_owner_pid(pid_path) == 12345


def test_read_owner_pid_missing_returns_none(tmp_path: Path) -> None:
    assert read_owner_pid(tmp_path / "nope.pid") is None


def test_write_heartbeat_roundtrip(tmp_path: Path) -> None:
    state_path = tmp_path / "memory.state.json"
    h = Heartbeat(pid=12345, started_at=1700000000.0, scope="mywiki:apply_plan")
    write_heartbeat(state_path, h)
    out = read_heartbeat(state_path)
    assert out is not None
    assert out.pid == 12345
    assert out.started_at == 1700000000.0
    assert out.scope == "mywiki:apply_plan"


def test_read_heartbeat_missing_returns_none(tmp_path: Path) -> None:
    assert read_heartbeat(tmp_path / "nope.state.json") is None


def test_read_heartbeat_corrupt_returns_none(tmp_path: Path) -> None:
    bad = tmp_path / "corrupt.state.json"
    bad.write_text("not json", encoding="utf-8")
    assert read_heartbeat(bad) is None
