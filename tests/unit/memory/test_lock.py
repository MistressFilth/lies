"""Unit tests for the cross-process flock on the wiki memory lock file."""

from __future__ import annotations

import fcntl
import subprocess
from pathlib import Path

import pytest

from lies.memory.models import WikiLockBusy
from lies.memory.service import WikiMemoryService
from lies.wiki.layout import WikiLayout


@pytest.fixture
def git_wiki(tmp_path: Path) -> WikiLayout:
    root = tmp_path / "wiki"
    for sub in ("wiki", ".lies", "raw"):
        (root / sub).mkdir(parents=True)
    (root / "wiki" / "concepts").mkdir(parents=True)
    (root / "wiki" / "index.md").write_text("# Index\n", encoding="utf-8")
    subprocess.run(["git", "init", "--initial-branch=main", str(root)], check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True)
    return WikiLayout(root)


def test_lock_path_is_under_lies_dir(git_wiki: WikiLayout) -> None:
    service = WikiMemoryService(git_wiki)
    path = service._lock_path()
    assert path == git_wiki.root / ".lies" / "memory.lock"


def test_acquire_flock_succeeds_when_unheld(git_wiki: WikiLayout) -> None:
    service = WikiMemoryService(git_wiki)
    with service._acquire_flock():
        assert service._lock_path().exists()


def test_acquire_flock_raises_busy_when_held(git_wiki: WikiLayout) -> None:
    service = WikiMemoryService(git_wiki)
    holder = service._lock_path().open("w", encoding="utf-8")
    try:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(WikiLockBusy), service._acquire_flock():
            pass  # pragma: no cover
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()


def test_acquire_flock_proceeds_after_release(git_wiki: WikiLayout) -> None:
    service = WikiMemoryService(git_wiki)
    holder = service._lock_path().open("w", encoding="utf-8")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    holder.close()
    with service._acquire_flock():
        pass


def test_acquire_flock_releases_on_exception(git_wiki: WikiLayout) -> None:
    service = WikiMemoryService(git_wiki)
    with pytest.raises(RuntimeError), service._acquire_flock():
        raise RuntimeError("boom")
    with service._acquire_flock():
        pass


def test_gitignore_contains_lock_entry(git_wiki: WikiLayout) -> None:
    """The target wiki's .gitignore contains the lock entry."""
    service = WikiMemoryService(git_wiki)
    with service._acquire_flock():
        pass
    text = (git_wiki.root / ".gitignore").read_text(encoding="utf-8")
    assert ".lies/memory.lock" in text


def test_gitignore_entry_is_idempotent(git_wiki: WikiLayout) -> None:
    """Calling _acquire_flock twice does not duplicate the entry."""
    service = WikiMemoryService(git_wiki)
    with service._acquire_flock():
        pass
    text1 = (git_wiki.root / ".gitignore").read_text(encoding="utf-8")
    with service._acquire_flock():
        pass
    text2 = (git_wiki.root / ".gitignore").read_text(encoding="utf-8")
    assert text1 == text2
    assert text2.count(".lies/memory.lock") == 1


def test_stash_does_not_unlink_held_lock(git_wiki: WikiLayout) -> None:
    """Regression: ``git stash --include-untracked`` must not unlink the lock.

    Without ``.lies/memory.lock`` in the target wiki's .gitignore, the
    stash would move the file aside. The kernel flock survives on the
    orphaned inode, but a concurrent process could then create a new
    file at the same path and flock the new inode, reopening the race.
    With the gitignore entry in place, the stash skips the file and
    the held flock remains effective against a fresh fd on the same
    path.
    """
    service = WikiMemoryService(git_wiki)
    with service._acquire_flock():
        lock_path = service._lock_path()
        assert lock_path.exists()

        result = subprocess.run(
            ["git", "stash", "push", "--include-untracked", "-m", "test"],
            cwd=git_wiki.root,
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


def test_wiki_lock_busy_is_wiki_memory_error() -> None:
    from lies.memory.models import WikiMemoryError

    assert issubclass(WikiLockBusy, WikiMemoryError)
