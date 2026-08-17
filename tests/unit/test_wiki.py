"""Tests for the Wiki dataclass."""

from __future__ import annotations

import pytest

from lies.errors import WikiNameError, WikiNotRegistered
from lies.wiki.wiki import Wiki


def _patch_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))


def test_data_root_for_uses_xdg_data_home(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _patch_xdg(monkeypatch, tmp_path)
    assert Wiki.data_root_for("mywiki") == tmp_path / "data" / "lies" / "mywiki"


def test_data_root_for_validates_name(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _patch_xdg(monkeypatch, tmp_path)
    with pytest.raises(WikiNameError):
        Wiki.data_root_for("foo/bar")


def test_require_raises_when_unregistered(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _patch_xdg(monkeypatch, tmp_path)
    with pytest.raises(WikiNotRegistered) as exc:
        Wiki.require("ghost")
    assert exc.value.name == "ghost"


def test_require_succeeds_after_data_root_mkdir(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _patch_xdg(monkeypatch, tmp_path)
    Wiki.data_root_for("mywiki").mkdir(parents=True)
    wiki = Wiki.require("mywiki")
    assert wiki.name == "mywiki"
    assert wiki.raw_dir == wiki.data_root / "raw"
    assert wiki.wiki_dir == wiki.data_root / "wiki"
    assert wiki.schema_path == wiki.config_root / "schema.md"
    assert wiki.collections_dir == wiki.config_root / "collections"
    assert wiki.hashes_dir == wiki.cache_root / "hashes"
    assert wiki.logs_dir == wiki.state_root / "logs"
    assert wiki.scratch_dir == wiki.state_root / "scratch"
    assert wiki.poison_root == wiki.state_root / "poison"
    assert wiki.memory_lock_path == wiki.runtime_root / "memory.lock"
    assert wiki.sync_lock_path == wiki.runtime_root / "sync.lock"
    assert wiki.sync_create_lock_path == wiki.runtime_root / "sync.lock.create"
    assert wiki.sync_fd_path == wiki.runtime_root / "sync.lock.fd"
    assert wiki.mcp_pid_path == wiki.runtime_root / "mcp.pid"
    assert wiki.mcp_create_lock_path == wiki.runtime_root / "mcp.pid.create"
    assert wiki.mcp_log_path == wiki.state_root / "mcp.log"


def test_wiki_memory_lock_paths_use_runtime_root(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _patch_xdg(monkeypatch, tmp_path)
    Wiki.data_root_for("mywiki").mkdir(parents=True)
    wiki = Wiki.require("mywiki")
    expected_base = tmp_path / "runtime" / "lies" / "mywiki"
    assert wiki.memory_create_lock_path == expected_base / "memory.lock.create"
    assert wiki.memory_pid_path == expected_base / "memory.pid"
    assert wiki.memory_heartbeat_path == expected_base / "memory.state.json"
