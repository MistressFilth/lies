"""Unit tests for the cross-process flock on the wiki memory lock file."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from lies import xdg
from lies.memory.models import WikiLockBusy
from lies.memory.service import WikiMemoryService
from lies.utils.lock_heartbeat import Heartbeat, write_heartbeat, write_owner_pid
from lies.wiki.layout import WikiLayout
from lies.wiki.wiki import Wiki


@pytest.fixture
def git_wiki(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Wiki:
    """Build a Wiki rooted in ``tmp_path`` with all five XDG roots under it.

    The memory lock lives under ``$XDG_RUNTIME_DIR/lies/<wiki>/memory.lock``
    since the XDG migration; this fixture points all five XDG role roots
    at ``tmp_path`` siblings so the lock path lives next to the data
    root and the test stays self-contained.
    """
    monkeypatch.setenv("LIES_XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("LIES_XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("LIES_XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("LIES_XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("LIES_XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    name = "test"
    data_root = Wiki.data_root_for(name)
    wiki = Wiki(
        name=name,
        data_root=data_root,
        config_root=xdg.config_home() / "lies" / name,
        cache_root=xdg.cache_home() / "lies" / name,
        state_root=xdg.state_home() / "lies" / name,
        runtime_root=xdg.runtime_dir_for(name),
    )
    layout = WikiLayout(wiki.data_root)
    layout.root.mkdir(parents=True, exist_ok=True)
    layout.raw_dir.mkdir(parents=True, exist_ok=True)
    layout.wiki_dir.mkdir(parents=True, exist_ok=True)
    (layout.wiki_dir / "concepts").mkdir(parents=True, exist_ok=True)
    (layout.wiki_dir / "index.md").write_text("# Index\n", encoding="utf-8")
    wiki.runtime_root.mkdir(parents=True, exist_ok=True)
    import subprocess

    subprocess.run(["git", "init", "--initial-branch=main", str(layout.root)], check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=layout.root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=layout.root, check=True)
    subprocess.run(["git", "add", "."], cwd=layout.root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=layout.root, check=True)
    return wiki


def test_lock_path_is_under_runtime_dir(git_wiki: Wiki) -> None:
    """The envelope lock (atomic-create sentinel) lives under the wiki's XDG runtime dir."""
    create_lock = git_wiki.memory_create_lock_path
    assert create_lock == git_wiki.runtime_root / "memory.lock.create"
    assert create_lock.parent == git_wiki.runtime_root


def test_acquire_flock_succeeds_when_unheld(git_wiki: Wiki) -> None:
    """After acquire, the create-lock sentinel + heartbeat envelope is on disk."""
    service = WikiMemoryService(git_wiki)
    with service._acquire_flock():
        assert git_wiki.memory_create_lock_path.exists()
        assert git_wiki.memory_pid_path.exists()
        assert git_wiki.memory_heartbeat_path.exists()
        # Sanity: the freshly-written heartbeat's pid is the holder (this process).
        hb = Heartbeat(
            pid=os.getpid(),
            started_at=time.time(),
            scope=git_wiki.name,
        )
        assert hb.pid > 0


def test_acquire_flock_raises_busy_when_held(
    git_wiki: Wiki, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live contender's create-lock blocks acquire; service surfaces ``WikiLockBusy``.

    The underlying :func:`acquire_create_lock` reap loop auto-recovers from
    a dead holder; simulating a still-alive foreign process requires a
    second OS process. We patch ``acquire_create_lock`` to return ``None``
    directly so this test exercises the ``WikiLockBusy`` wrapping in
    ``_acquire_wiki_flock`` without spawning a subprocess.
    """
    service = WikiMemoryService(git_wiki)
    monkeypatch.setattr(
        "lies.memory.service.acquire_create_lock",
        lambda *_a, **_kw: None,
    )
    with pytest.raises(WikiLockBusy), service._acquire_flock():
        pass  # pragma: no cover


def test_acquire_flock_proceeds_after_release(git_wiki: Wiki) -> None:
    """A prior holder's stale create-lock is reaped + the next acquire succeeds."""
    service = WikiMemoryService(git_wiki)
    create_lock = git_wiki.memory_create_lock_path
    pid_path = git_wiki.memory_pid_path
    state_path = git_wiki.memory_heartbeat_path

    create_lock.parent.mkdir(parents=True, exist_ok=True)
    # Seed a dead holder (pid 999999 is never alive in this process).
    create_lock.touch()
    write_owner_pid(pid_path, 999999)
    write_heartbeat(
        state_path,
        Heartbeat(pid=999999, started_at=time.time(), scope=git_wiki.name),
    )

    # The reap loop should detect the dead pid, unlink the envelope, and let
    # the retry succeed.
    with service._acquire_flock():
        assert create_lock.exists()


def test_acquire_flock_releases_on_exception(git_wiki: Wiki) -> None:
    service = WikiMemoryService(git_wiki)
    with pytest.raises(RuntimeError), service._acquire_flock():
        raise RuntimeError("boom")
    with service._acquire_flock():
        pass


def test_lock_path_is_not_in_wiki_data_root(git_wiki: Wiki) -> None:
    """The envelope lock lives under XDG_RUNTIME_DIR, not under the wiki's data root.

    No ``.lies/`` directory exists in the XDG layout — the lock cannot
    live under the wiki anymore, so this is the structural invariant
    that replaced the old ``.lies/memory.lock`` gitignore check.
    """
    create_lock = git_wiki.memory_create_lock_path
    assert not str(create_lock).startswith(str(git_wiki.data_root))
    assert not (git_wiki.data_root / ".lies").exists()


def test_stash_does_not_unlink_held_lock(git_wiki: Wiki) -> None:
    """Regression: ``git stash --include-untracked`` must not unlink the lock.

    Under the XDG layout the lock lives outside the working tree
    (``$XDG_RUNTIME_DIR``), so even an untracked ``.gitignore`` exclusion
    is unnecessary: there is no git coordinate file to escape.
    """
    import subprocess

    service = WikiMemoryService(git_wiki)
    with service._acquire_flock():
        create_lock = git_wiki.memory_create_lock_path
        pid_path = git_wiki.memory_pid_path
        state_path = git_wiki.memory_heartbeat_path
        assert create_lock.exists()

        result = subprocess.run(
            ["git", "stash", "push", "--include-untracked", "-m", "test"],
            cwd=git_wiki.data_root,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, f"stash failed: {result.stderr}"

        # The create-lock + pid + heartbeat are still on disk (none of them
        # live in the wiki's working tree, so stash cannot reach them).
        assert create_lock.exists(), "create-lock was unlinked by stash"
        assert pid_path.exists(), "pid file was unlinked by stash"
        assert state_path.exists(), "heartbeat was unlinked by stash"


def test_wiki_lock_busy_is_wiki_flock_error() -> None:
    from lies.lock_errors import WikiFlockError

    assert issubclass(WikiLockBusy, WikiFlockError)
