"""LibraryWriter surfaces QmdLockBusy distinctly from generic qmd-spawn failures."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lies.library.paths import Library
from lies.library.writer import LibraryWriter
from lies.lock_errors import QmdLockBusy


@pytest.fixture
def lib(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Library:
    """Redirect ``xdg.data_home`` so ``Library.open()`` returns a per-test Library."""
    from lies import xdg

    monkeypatch.setattr(xdg, "data_home", lambda: tmp_path)
    Library.open.cache_clear()
    yield Library.open()


@pytest.fixture
def lib_with_git(lib: Library) -> Library:
    """Initialise a git repo at ``library.git_root`` with a baseline commit.

    Mirror of the canonical fixture in ``test_writer.py`` /
    ``test_qmd_hook.py``: ``.gitkeep`` placeholder inside the empty
    ``.lies/`` so ``git add .`` has something to stage.
    """
    lib.git_root.mkdir(parents=True, exist_ok=True)
    (lib.git_root / ".lies").mkdir(parents=True, exist_ok=True)
    (lib.git_root / ".gitkeep").write_text("")
    subprocess.run(
        ["git", "init", "-b", "main", str(lib.git_root)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(lib.git_root), "config", "user.email", "t@t"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(lib.git_root), "config", "user.name", "t"],
        check=True,
    )
    subprocess.run(["git", "-C", str(lib.git_root), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(lib.git_root), "commit", "-m", "init"],
        check=True,
        capture_output=True,
    )
    return lib


def test_writer_re_raises_QmdLockBusy_with_holder_pid(
    lib_with_git: Library,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LibraryWriter.commit re-raises QmdLockBusy so the operator sees the holder PID.

    The qmd embed helper raises a ``QmdLockBusy`` carrying
    ``holder_pid=4242``. ``LibraryWriter.commit`` must re-raise the same
    exception class so the MCP / CLI caller surfaces an
    operator-actionable message ("qmd flock contention, holder PID 4242,
    waited 30.0s, max 30.0s — wait and retry") instead of the generic
    "warning: qmd embed failed" swallowed stderr line.

    TODAY the broad ``except Exception`` catches ``QmdLockBusy`` and the
    test fails with ``Failed: DID NOT RAISE``.
    """
    sentinel = QmdLockBusy(holder_pid=4242, waited_s=30.0, max_s=30.0)
    coll_dir = lib_with_git.collections_root / "claude_platform"
    coll_dir.mkdir(parents=True, exist_ok=True)
    mirror = coll_dir / "x.md"
    mirror.write_text("body\n")

    writer = LibraryWriter(lib_with_git)
    monkeypatch.setattr("lies.library.writer.qmd_embed", MagicMock(side_effect=sentinel))

    with pytest.raises(QmdLockBusy) as excinfo:
        writer.commit(
            [mirror.relative_to(lib_with_git.git_root)],
            message="t",
            qmd_collection="claude_platform",
        )
    assert excinfo.value.holder_pid == 4242
    assert "4242" in str(excinfo.value)


def test_writer_swallows_other_qmd_failures_unchanged(
    lib_with_git: Library,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-QmdLockBusy failures keep the swallow-and-warn shape.

    CUDA OOM and qmd daemon down are transient derived-index outages;
    the library commit is authoritative and must land even when qmd
    fails. The split must not promote *every* qmd error to a re-raise;
    only ``QmdLockBusy`` gets that treatment. Pins the existing
    swallow-and-warn invariant against an over-eager refactor.
    """
    cuda_oom = RuntimeError("cuMemAddressReserve: out of memory")
    coll_dir = lib_with_git.collections_root / "claude_platform"
    coll_dir.mkdir(parents=True, exist_ok=True)
    mirror = coll_dir / "x.md"
    mirror.write_text("body\n")

    writer = LibraryWriter(lib_with_git)
    monkeypatch.setattr("lies.library.writer.qmd_embed", MagicMock(side_effect=cuda_oom))

    # Should NOT raise — the commit must land despite qmd failure.
    result = writer.commit(
        [mirror.relative_to(lib_with_git.git_root)],
        message="t",
        qmd_collection="claude_platform",
    )
    # LibraryWriter.commit returns the new commit SHA (str) on success or
    # None on a no-op; either is acceptable here because the contract is
    # "no exception" and "derived-index outage does not roll back".
    assert result is None or isinstance(result, str)


def test_the_reraise_carries_the_lock_path(
    lib_with_git: Library,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The re-raised ``QmdLockBusy`` names the inode, not just the holder."""
    monkeypatch.setenv("LIES_QMD_LOCK_PATH", str(tmp_path / "qmd.lock"))
    inode = str(tmp_path / "qmd.lock")
    sentinel = QmdLockBusy(
        holder_pid=4242,
        waited_s=120.0,
        max_s=120.0,
        lock_path=inode,
    )
    assert sentinel.lock_path == inode, "precondition: the class stores the path"

    coll_dir = lib_with_git.collections_root / "claude_platform"
    coll_dir.mkdir(parents=True, exist_ok=True)
    mirror = coll_dir / "x.md"
    mirror.write_text("body\n")

    writer = LibraryWriter(lib_with_git)
    monkeypatch.setattr("lies.library.writer.qmd_embed", MagicMock(side_effect=sentinel))

    with pytest.raises(QmdLockBusy) as excinfo:
        writer.commit(
            [mirror.relative_to(lib_with_git.git_root)],
            message="t",
            qmd_collection="claude_platform",
        )

    raised = excinfo.value
    assert raised.lock_path == inode, (
        f"the re-raise dropped lock_path: got {raised.lock_path!r}, want "
        f"{inode!r}. writer.commit is the one production raiser that "
        f"re-wraps an exception it caught, so the value is already in hand."
    )
    # The rest of the triad must survive too — a fix that threads the
    # path through but rebuilds the message from defaults would be worse
    # than the original.
    assert raised.holder_pid == 4242
    assert raised.waited_s == 120.0
    assert raised.max_s == 120.0
    text = str(raised)
    assert inode in text, f"the message must name the contended inode: {text!r}"
    assert "4242" in text and "120" in text, (
        f"the message must still name the holder, the wait and the budget: {text!r}"
    )
