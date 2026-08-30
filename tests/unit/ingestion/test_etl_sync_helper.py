"""Tests for the sync_helper orchestration module."""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest import mock

import pytest

from lies import xdg
from lies.etl.heartbeat import Heartbeat
from lies.etl.sync_helper import acquire_heartbeat, release_heartbeat
from lies.lock_errors import WikiFlockIndeterminate
from lies.utils.lock_heartbeat import AcquireResult
from lies.wiki.wiki import Wiki


@pytest.fixture
def wiki(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Wiki:
    """A Wiki with all five XDG roots under ``tmp_path`` so tests are hermetic."""
    monkeypatch.setenv("LIES_XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("LIES_XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("LIES_XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("LIES_XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("LIES_XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    name = "test"
    wiki = Wiki(
        name=name,
        data_root=Wiki.data_root_for(name),
        config_root=xdg.config_home() / "lies" / name,
        cache_root=xdg.cache_home() / "lies" / name,
        state_root=xdg.state_home() / "lies" / name,
        runtime_root=xdg.runtime_dir_for(name),
    )
    wiki.runtime_root.mkdir(parents=True, exist_ok=True)
    return wiki


def test_acquire_heartbeat_writes_and_releases(wiki: Wiki) -> None:
    """acquire_heartbeat writes the heartbeat; release clears it."""
    hb = acquire_heartbeat(wiki, wait=False, fail_busy=True)
    assert hb is not None
    assert hb.pid == os.getpid()
    # The sibling lock file must exist while we hold the heartbeat.
    assert wiki.sync_create_lock_path.exists()
    release_heartbeat(wiki)
    assert not wiki.sync_create_lock_path.exists()
    assert not wiki.sync_lock_path.exists()


def test_acquire_heartbeat_returns_none_when_busy(wiki: Wiki) -> None:
    """Two concurrent acquirers cannot both win the create lock."""
    busy = Heartbeat(pid=os.getpid(), started_at=time.time(), collection="other")
    with mock.patch("lies.etl.sync_helper.read_heartbeat", return_value=busy):
        hb = acquire_heartbeat(wiki, wait=False, fail_busy=True)
    assert hb is None


def test_acquire_heartbeat_atomic_under_concurrent_acquire(wiki: Wiki) -> None:
    """Two concurrent acquire_create_lock calls cannot both succeed.

    This exercises the OS-level O_EXCL guarantee that backs the
    TOCTOU-safe acquire path. The first wins the create; the second
    gets FileExistsError → None.
    """
    from lies.etl.heartbeat import acquire_create_lock, release_create_lock

    fd1 = acquire_create_lock(wiki)
    fd2 = acquire_create_lock(wiki)
    assert fd1 is not None
    assert fd2 is None
    release_create_lock(wiki, fd1)


def test_release_heartbeat_no_held_lock(wiki: Wiki) -> None:
    """release_heartbeat is safe even if the lock file is absent."""
    # No acquire first; release should be a no-op (no raise).
    release_heartbeat(wiki)


def test_acquire_heartbeat_routes_indeterminate_to_wiki_flock_indeterminate(
    wiki: Wiki,
) -> None:
    """An indeterminate acquire result surfaces as WikiFlockIndeterminate."""
    indeterminate = AcquireResult(
        fd=-1,
        holder_pid=999,
        holder_started_at=1723828800.0,
        status="indeterminate",
    )
    with (
        mock.patch("lies.etl.sync_helper.acquire_create_lock", return_value=indeterminate),
        pytest.raises(WikiFlockIndeterminate) as caught,
    ):
        acquire_heartbeat(wiki, wait=False, fail_busy=True)
    msg = str(caught.value)
    assert "pid 999" in msg
    assert "force-repair" in msg
