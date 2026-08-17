"""Tests for xdg path resolution."""

from __future__ import annotations

import os
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


# NOTE: These two tests rely on `chmod 0o000` on a parent directory to
# trigger EACCES. On a host with `fs.protected_regular` or similar sysctl
# restrictions, the unprivileged chmod may not be enough to deny the
# caller; the test then passes for the wrong reason (mkdir succeeds and
# the XDG_RUNTIME_DIR branch is taken). The skip-if-root guard catches
# the most common cause; consider switching to a mocking-based permission
# denial (e.g., monkeypatching Path.mkdir to raise PermissionError) if
# the test ever drifts.
@pytest.mark.skipif(os.geteuid() == 0, reason="chmod 0o000 does not restrict root")
def test_runtime_dir_falls_back_on_permission_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``XDG_RUNTIME_DIR`` mkdir on an unwritable parent must fall through
    to ``<state_home>/run`` per the module's best-effort contract."""
    locked_parent = tmp_path / "no-perm"
    locked_parent.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(locked_parent / "runtime"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    try:
        locked_parent.chmod(0o000)
        rt = xdg.runtime_dir()
    finally:
        locked_parent.chmod(0o700)
    assert rt == tmp_path / "state" / "run"


@pytest.mark.skipif(os.geteuid() == 0, reason="chmod 0o000 does not restrict root")
# See the sysctl-fragility note above the first permission-error test
# (around line 83) — both chmod-based tests share the same EACCES mechanism.
@pytest.mark.skipif(os.geteuid() == 0, reason="chmod 0o000 does not restrict root")
def test_runtime_dir_falls_back_on_permission_error_for_lies_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``LIES_XDG_RUNTIME_DIR`` mkdir on an unwritable parent must fall
    through to ``<state_home>/run``."""
    locked_parent = tmp_path / "no-perm"
    locked_parent.mkdir()
    monkeypatch.setenv("LIES_XDG_RUNTIME_DIR", str(locked_parent / "runtime"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    try:
        locked_parent.chmod(0o000)
        rt = xdg.runtime_dir()
    finally:
        locked_parent.chmod(0o700)
    assert rt == tmp_path / "state" / "run"
