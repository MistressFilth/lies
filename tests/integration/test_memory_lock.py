"""Cross-process contention test for the wiki memory flock."""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from lies import xdg
from lies.memory.models import (
    MemoryPlan,
    PageCreate,
    WikiLockBusy,
)
from lies.memory.service import WikiMemoryService
from lies.wiki.layout import WikiLayout
from lies.wiki.wiki import Wiki


@pytest.fixture
def git_wiki(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Wiki:
    """Build a Wiki rooted in ``tmp_path`` with all five XDG roots under it.

    Matches the unit-test fixture so the integration test exercises the
    XDG runtime path (``$XDG_RUNTIME_DIR/lies/<wiki>/memory.lock``) the
    way ``lies init`` would produce it after the migration.
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
    subprocess.run(["git", "init", "--initial-branch=main", str(layout.root)], check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=layout.root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=layout.root, check=True)
    subprocess.run(["git", "add", "."], cwd=layout.root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=layout.root, check=True)
    return wiki


HOLDER_SCRIPT = textwrap.dedent(
    """
    import os, sys, time
    from pathlib import Path
    from lies.utils.exclusive import acquire_create_lock
    from lies.utils.lock_heartbeat import Heartbeat, write_heartbeat, write_owner_pid

    create_lock = Path(sys.argv[1])
    pid_path = Path(sys.argv[2])
    state_path = Path(sys.argv[3])
    ready_marker = Path(sys.argv[4])

    create_lock.parent.mkdir(parents=True, exist_ok=True)
    fd = acquire_create_lock(
        create_lock,
        max_age_s=7200,
        pid_path=pid_path,
        state_json_path=state_path,
    )
    if fd is None:
        sys.exit(2)
    write_owner_pid(pid_path, os.getpid())
    write_heartbeat(
        state_path,
        Heartbeat(pid=os.getpid(), started_at=time.time(), scope=""),
    )
    ready_marker.write_text("ready", encoding="utf-8")
    time.sleep(15)
    """
)


def _wait_for_holder(ready_marker: Path, *, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while not ready_marker.exists():
        if time.time() > deadline:
            pytest.fail("holder process did not signal ready in time")
        time.sleep(0.05)


def test_apply_plan_raises_wiki_lock_busy_when_other_process_holds_lock(
    git_wiki: Wiki, tmp_path: Path
) -> None:
    create_lock = git_wiki.memory_create_lock_path
    pid_path = git_wiki.memory_pid_path
    state_path = git_wiki.memory_heartbeat_path
    ready_marker = tmp_path / "holder.ready"

    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            HOLDER_SCRIPT,
            str(create_lock),
            str(pid_path),
            str(state_path),
            str(ready_marker),
        ],
    )
    try:
        _wait_for_holder(ready_marker)

        service = WikiMemoryService(git_wiki)
        service.register_evidence({"page-1"})
        plan = MemoryPlan(
            operations=[
                PageCreate(
                    path="concepts/example.md",
                    content=("---\ntitle: Example\ntype: concept\n---\n# Example\n"),
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
    git_wiki: Wiki, tmp_path: Path
) -> None:
    create_lock = git_wiki.memory_create_lock_path
    pid_path = git_wiki.memory_pid_path
    state_path = git_wiki.memory_heartbeat_path
    ready_marker = tmp_path / "holder.ready"

    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            HOLDER_SCRIPT,
            str(create_lock),
            str(pid_path),
            str(state_path),
            str(ready_marker),
        ],
    )
    try:
        _wait_for_holder(ready_marker)

        # Force the holder to exit (release_create_lock unlinks the
        # create-lock + pid + heartbeat; the OS closes the O_EXCL fd).
        holder.terminate()
        holder.wait(timeout=10)

        service = WikiMemoryService(git_wiki)
        service.register_evidence({"page-1"})
        plan = MemoryPlan(
            operations=[
                PageCreate(
                    path="concepts/example.md",
                    content=("---\ntitle: Example\ntype: concept\n---\n# Example\n"),
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
