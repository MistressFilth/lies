"""Tests for xdg path resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from lies import xdg


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in [
        "LIES_XDG_DATA_HOME",
        "LIES_XDG_CONFIG_HOME",
        "LIES_XDG_CACHE_HOME",
        "LIES_XDG_STATE_HOME",
        "LIES_XDG_RUNTIME_DIR",
        "XDG_DATA_HOME",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "XDG_RUNTIME_DIR",
    ]:
        monkeypatch.delenv(k, raising=False)


def test_data_home_uses_lies_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIES_XDG_DATA_HOME", "/custom/data")
    assert xdg.data_home() == Path("/custom/data")


def test_data_home_uses_xdg_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", "/xdg/data")
    assert xdg.data_home() == Path("/xdg/data")


def test_data_home_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "/home/foo")
    assert xdg.data_home() == Path("/home/foo/.local/share")


def test_runtime_dir_unset_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", "/state")
    # XDG_RUNTIME_DIR unset
    rt = xdg.runtime_dir()
    assert rt == Path("/state/run")


def test_runtime_dir_for_per_wiki(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_root))
    assert xdg.runtime_dir_for("mywiki") == runtime_root / "lies" / "mywiki"


def test_runtime_dir_for_unset_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", "/state")
    assert xdg.runtime_dir_for("mywiki") == Path("/state/run/lies/mywiki")


def test_xdg_root_precedence_lies_over_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIES_XDG_CACHE_HOME", "/lies/cache")
    monkeypatch.setenv("XDG_CACHE_HOME", "/xdg/cache")
    assert xdg._xdg_root("XDG_CACHE_HOME", "/default") == Path("/lies/cache")


def test_runtime_dir_uses_lies_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LIES_XDG_RUNTIME_DIR", str(tmp_path / "lies-runtime"))
    assert xdg.runtime_dir() == tmp_path / "lies-runtime"
    assert (tmp_path / "lies-runtime").is_dir()


def test_lies_xdg_runtime_dir_wins_over_xdg_runtime_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LIES_XDG_RUNTIME_DIR", str(tmp_path / "lies-runtime"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "xdg-runtime"))
    assert xdg.runtime_dir() == tmp_path / "lies-runtime"
