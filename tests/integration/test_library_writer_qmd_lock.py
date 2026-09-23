"""LibraryWriter surfaces QmdLockBusy distinctly from generic qmd-spawn failures.

The post-commit qmd hook in ``LibraryWriter.commit`` runs ``qmd_embed``
inside a broad ``except Exception`` so transient derived-index outages
(CUDA OOM, qmd daemon down) never roll back the library commit. That
shape is right for transient failures but wrong for site-wide qmd flock
contention: a ``QmdLockBusy`` carries operator-actionable context
(holder PID, wait/max seconds) and should reach the CLI / MCP caller as
a distinct typed error, not as a swallowed ``warning: qmd embed
failed`` line on stderr.

Two tests pin the split:

1. ``test_writer_re_raises_QmdLockBusy_with_holder_pid`` — when the
   qmd embed helper raises ``QmdLockBusy``, ``LibraryWriter.commit``
   must re-raise the same exception class with the holder PID preserved
   so the operator sees an actionable message instead of a swallowed
   warning. TODAY this fails: the broad ``except Exception`` catches
   ``QmdLockBusy`` and prints the generic warning.
2. ``test_writer_swallows_other_qmd_failures_unchanged`` — non-lock
   failures (CUDA OOM, qmd crash) keep the swallow-and-warn shape so
   derived-index outages never roll back the library commit. TODAY this
   passes; the test pins the invariant against an over-eager refactor
   that promotes *all* qmd errors to re-raise.

The pattern mirrors ``tests/unit/library/test_qmd_hook.py``: a real
``lib_with_git`` fixture (so ``atomic_commit`` lands), the file written
to disk (so ``git add`` succeeds), and ``monkeypatch.setattr(
"lies.library.writer.qmd_embed", ...)`` which both triggers the
:PEP:`562` ``__getattr__`` (writes the real function into module
globals) and replaces it with the stub.
"""

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
