"""Unit tests for the cross-process flock on the wiki memory lock file."""

from __future__ import annotations

import fcntl
from pathlib import Path

import pytest

from lies import xdg
from lies.memory.models import WikiLockBusy
from lies.memory.service import WikiMemoryService
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
    service = WikiMemoryService(git_wiki)
    path = service._lock_path()
    assert path == git_wiki.runtime_root / "memory.lock"


def test_acquire_flock_succeeds_when_unheld(git_wiki: Wiki) -> None:
    service = WikiMemoryService(git_wiki)
    with service._acquire_flock():
        assert service._lock_path().exists()


def test_acquire_flock_raises_busy_when_held(git_wiki: Wiki) -> None:
    service = WikiMemoryService(git_wiki)
    holder = service._lock_path().open("w", encoding="utf-8")
    try:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(WikiLockBusy), service._acquire_flock():
            pass  # pragma: no cover
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()


def test_acquire_flock_proceeds_after_release(git_wiki: Wiki) -> None:
    service = WikiMemoryService(git_wiki)
    holder = service._lock_path().open("w", encoding="utf-8")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    holder.close()
    with service._acquire_flock():
        pass


def test_acquire_flock_releases_on_exception(git_wiki: Wiki) -> None:
    service = WikiMemoryService(git_wiki)
    with pytest.raises(RuntimeError), service._acquire_flock():
        raise RuntimeError("boom")
    with service._acquire_flock():
        pass


def test_lock_path_is_not_in_wiki_data_root(git_wiki: Wiki) -> None:
    """The lock lives under XDG_RUNTIME_DIR, not under the wiki's data root.

    No ``.lies/`` directory exists in the XDG layout — the lock cannot
    live under the wiki anymore, so this is the structural invariant
    that replaced the old ``.lies/memory.lock`` gitignore check.
    """
    service = WikiMemoryService(git_wiki)
    lock_path = service._lock_path()
    assert not str(lock_path).startswith(str(git_wiki.data_root))
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
        lock_path = service._lock_path()
        assert lock_path.exists()

        result = subprocess.run(
            ["git", "stash", "push", "--include-untracked", "-m", "test"],
            cwd=git_wiki.data_root,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, f"stash failed: {result.stderr}"

        # The lock file is still on disk (not unlinked into the stash).
        assert lock_path.exists(), "lock file was unlinked by stash"

        # A fresh fd on the same path still gets EWOULDBLOCK — the
        # original holder's flock is still effective.
        probe = lock_path.open("w", encoding="utf-8")
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            probe.close()


def test_wiki_lock_busy_is_wiki_flock_error() -> None:
    from lies.lock_errors import WikiFlockError

    assert issubclass(WikiLockBusy, WikiFlockError)
