"""Cross-process contention test for the wiki memory flock."""
from __future__ import annotations

import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from lies.memory.models import (
    MemoryPlan,
    PageCreate,
    WikiLockBusy,
)
from lies.memory.service import WikiMemoryService, _ensure_lock_gitignored
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
    # The holder subprocess below calls raw ``fcntl.flock`` without
    # going through ``_acquire_wiki_flock``, so the .gitignore entry
    # that prevents ``git stash push --include-untracked`` from
    # unlinking the lock file must be in place before we fork.
    _ensure_lock_gitignored(root / ".lies" / "memory.lock")
    return WikiLayout(root)


HOLDER_SCRIPT = textwrap.dedent(
    """
    import fcntl, sys, time
    from pathlib import Path
    lock_path = Path(sys.argv[1])
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = lock_path.open("w", encoding="utf-8")
    fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    # Signal parent that the lock is held.
    Path(sys.argv[2]).write_text("ready", encoding="utf-8")
    time.sleep(15)
    fd.close()
    """
)


def _wait_for_holder(ready_marker: Path, *, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while not ready_marker.exists():
        if time.time() > deadline:
            pytest.fail("holder process did not signal ready in time")
        time.sleep(0.05)


def test_apply_plan_raises_wiki_lock_busy_when_other_process_holds_lock(
    git_wiki: WikiLayout, tmp_path: Path
) -> None:
    lock_path = git_wiki.root / ".lies" / "memory.lock"
    ready_marker = tmp_path / "holder.ready"

    holder = subprocess.Popen(
        [sys.executable, "-c", HOLDER_SCRIPT, str(lock_path), str(ready_marker)],
    )
    try:
        _wait_for_holder(ready_marker)

        service = WikiMemoryService(git_wiki)
        service.register_evidence({"page-1"})
        plan = MemoryPlan(
            operations=[
                PageCreate(
                    path="concepts/example.md",
                    content=(
                        "---\ntitle: Example\ntype: concept\n---\n# Example\n"
                    ),
                    evidence=["page-1"],
                )
            ],
            rationale="new concept",
            evidence=["page-1"],
        )
        with pytest.raises(WikiLockBusy):
            service.apply_plan(plan)

        # The wiki state is unchanged (no partial write).
        assert not (git_wiki.wiki_dir / "concepts" / "example.md").exists()
    finally:
        holder.terminate()
        holder.wait(timeout=10)


def test_apply_plan_succeeds_after_other_process_releases_lock(
    git_wiki: WikiLayout, tmp_path: Path
) -> None:
    lock_path = git_wiki.root / ".lies" / "memory.lock"
    ready_marker = tmp_path / "holder.ready"

    holder = subprocess.Popen(
        [sys.executable, "-c", HOLDER_SCRIPT, str(lock_path), str(ready_marker)],
    )
    try:
        _wait_for_holder(ready_marker)

        # Force the holder to exit (kernel releases the flock on fd close).
        holder.terminate()
        holder.wait(timeout=10)

        service = WikiMemoryService(git_wiki)
        service.register_evidence({"page-1"})
        plan = MemoryPlan(
            operations=[
                PageCreate(
                    path="concepts/example.md",
                    content=(
                        "---\ntitle: Example\ntype: concept\n---\n# Example\n"
                    ),
                    evidence=["page-1"],
                )
            ],
            rationale="new concept",
            evidence=["page-1"],
        )
        receipt = service.apply_plan(plan)
        assert receipt.changed_pages
        assert (git_wiki.wiki_dir / "concepts" / "example.md").exists()
    finally:
        if holder.poll() is None:
            holder.terminate()
            holder.wait(timeout=10)
