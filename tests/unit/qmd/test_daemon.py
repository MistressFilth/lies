from __future__ import annotations

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
    qmd_daemon.ensure_qmd_daemon(data_dir=tmp_path / "fresh")
    assert reap_called == [True]
    assert spawn_called == [True]
    assert qmd_daemon.read_sidecar_data_dir() == tmp_path / "fresh"
