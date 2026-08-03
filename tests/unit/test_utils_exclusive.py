from __future__ import annotations

import os
import time
from pathlib import Path

from lies.utils.exclusive import (
    acquire_create_lock,
    ensure_gitignored,
    release_create_lock,
)


def test_acquire_returns_fd_when_free(tmp_path: Path) -> None:
    lock = tmp_path / "sub" / "thing.lock.create"
    fd = acquire_create_lock(lock, max_age_s=60.0)
    assert fd is not None
    assert lock.exists()
    release_create_lock(lock, fd)


def test_second_acquire_is_none(tmp_path: Path) -> None:
    lock = tmp_path / "thing.lock.create"
    first = acquire_create_lock(lock, max_age_s=60.0)
    second = acquire_create_lock(lock, max_age_s=60.0)
    assert first is not None
    assert second is None
    release_create_lock(lock, first)


def test_release_unlinks_and_reallows(tmp_path: Path) -> None:
    lock = tmp_path / "thing.lock.create"
    fd = acquire_create_lock(lock, max_age_s=60.0)
    release_create_lock(lock, fd)
    assert not lock.exists()
    again = acquire_create_lock(lock, max_age_s=60.0)
    assert again is not None
    release_create_lock(lock, again)


def test_orphan_reclaimed_past_window(tmp_path: Path) -> None:
    lock = tmp_path / "thing.lock.create"
    fd = acquire_create_lock(lock, max_age_s=60.0)
    assert fd is not None
    old = time.time() - 120
    os.utime(lock, (old, old))
    reclaimed = acquire_create_lock(lock, max_age_s=60.0)
    assert reclaimed is not None
    release_create_lock(lock, reclaimed)


def test_orphan_not_reclaimed_inside_window(tmp_path: Path) -> None:
    lock = tmp_path / "thing.lock.create"
    fd = acquire_create_lock(lock, max_age_s=3600.0)
    old = time.time() - 120
    os.utime(lock, (old, old))
    assert acquire_create_lock(lock, max_age_s=3600.0) is None
    release_create_lock(lock, fd)


def test_release_tolerates_none_fd_and_missing_file(tmp_path: Path) -> None:
    release_create_lock(tmp_path / "absent", None)


def test_ensure_gitignored_creates_file(tmp_path: Path) -> None:
    target = tmp_path / ".lies" / "mcp.pid"
    ensure_gitignored(target, wiki_root=tmp_path)
    assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == ".lies/mcp.pid\n"


def test_ensure_gitignored_is_idempotent(tmp_path: Path) -> None:
    target = tmp_path / ".lies" / "mcp.pid"
    ensure_gitignored(target, wiki_root=tmp_path)
    ensure_gitignored(target, wiki_root=tmp_path)
    assert (tmp_path / ".gitignore").read_text(encoding="utf-8").count("mcp.pid") == 1


def test_ensure_gitignored_appends_newline_when_missing(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("existing", encoding="utf-8")
    ensure_gitignored(tmp_path / ".lies" / "mcp.pid", wiki_root=tmp_path)
    assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == "existing\n.lies/mcp.pid\n"
