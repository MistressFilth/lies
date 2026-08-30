"""Integration: cross-process recovery of the memory flock.

Real subprocess coverage of reap paths the unit tests can't fake:
- kill -9 mid-write (test_process_crash_then_reap)
- wall-clock stale heartbeat with a live PID (test_wall_clock_stale_reaps)
- os._exit bypassing __exit__ (test_gc_cleanup_on_unexpected_exit)

Targets the v0.10.3 stale-lock recovery spec, Commit G.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from lies.utils.exclusive import acquire_create_lock, release_create_lock
from lies.utils.lock_heartbeat import Heartbeat, write_heartbeat, write_owner_pid
from lies.wiki.wiki import Wiki


@pytest.fixture
def fake_wiki(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Wiki:
    """Build a minimal wiki rooted at ``tmp_path/data/lies/mywiki``.

    The autouse ``_isolated_xdg`` fixture in ``tests/conftest.py`` already
    redirected XDG to ``tmp_path/xdg/<role>/``; this fixture overrides
    that to a flat ``tmp_path/<role>/`` layout so child subprocesses
    share storage with the parent. Monkeypatches
    ``Wiki.data_root_for`` to bind the wiki to ``tmp_path`` and creates
    the on-disk shape ``Wiki.require`` expects.
    """
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(
        "lies.wiki.wiki.Wiki.data_root_for",
        classmethod(lambda cls, name: tmp_path / "data" / "lies" / name),
    )
    (tmp_path / "data" / "lies" / "mywiki").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "lies" / "mywiki" / "raw").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "lies" / "mywiki" / "wiki").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "lies" / "mywiki" / ".git").mkdir(parents=True, exist_ok=True)
    return Wiki.require("mywiki")


# Child scripts. Inline rather than written to disk so they share the
# test's XDG env without managing script-file cleanup.
HOLDER_ACQUIRE_AND_SLEEP = textwrap.dedent(
    """
    import time
    from lies.memory.service import _acquire_wiki_flock
    from lies.wiki.wiki import Wiki

    wiki = Wiki.require("mywiki")
    ctx = _acquire_wiki_flock(wiki)
    ctx.__enter__()
    print("READY", flush=True)
    time.sleep(60)
    """
)

HOLDER_ACQUIRE_AND_ABORT = textwrap.dedent(
    """
    import os
    from lies.memory.service import _acquire_wiki_flock
    from lies.wiki.wiki import Wiki

    wiki = Wiki.require("mywiki")
    with _acquire_wiki_flock(wiki):
        print("READY", flush=True)
        os._exit(0)  # bypass __exit__: lock files stay on disk
    """
)


def _spawn_holder(script: str, tmp_path: Path) -> subprocess.Popen[str]:
    """Spawn the child script with the parent's environment.

    ``sys.executable`` is the venv Python (``uv run pytest`` activates it);
    ``lies`` is on its ``sys.path`` via the editable install, so the child
    imports ``lies.memory.service`` without any ``PYTHONPATH`` setup.
    """
    return subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _wait_for_ready(proc: subprocess.Popen[str]) -> None:
    """Block until the child prints READY on stdout; surface stderr on failure."""
    line = proc.stdout.readline().strip()
    if line != "READY":
        stderr = proc.stderr.read()
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        pytest.fail(f"child did not signal READY (got {line!r}); stderr: {stderr}")


def _cleanup_holder(proc: subprocess.Popen[str] | None) -> None:
    """Best-effort kill for a still-running holder, swallowing errors."""
    if proc is None:
        return
    if proc.poll() is None:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def test_process_crash_then_reap(tmp_path: Path, fake_wiki: Wiki) -> None:
    """Spawn a child holding the flock; ``kill -9`` it; reap-then-reacquire succeeds.

    The child writes ``memory.lock.create`` + ``memory.pid`` (its PID) +
    ``memory.state.json`` (heartbeat) and then sleeps forever. Killing
    the child with SIGKILL leaves those files on disk and dead-PID,
    which the reap path treats as a stale holder.
    """
    proc = _spawn_holder(HOLDER_ACQUIRE_AND_SLEEP, tmp_path)
    try:
        _wait_for_ready(proc)
        proc.kill()
        proc.wait(timeout=5)
    except BaseException:
        _cleanup_holder(proc)
        raise

    fd_result = acquire_create_lock(
        fake_wiki.memory_create_lock_path,
        max_age_s=7200,
        pid_path=fake_wiki.memory_pid_path,
        state_json_path=fake_wiki.memory_heartbeat_path,
    )
    assert fd_result is not None, "expected reap-then-reacquire after kill -9"
    release_create_lock(
        fake_wiki.memory_create_lock_path,
        fd_result.fd,
        pid_path=fake_wiki.memory_pid_path,
        state_json_path=fake_wiki.memory_heartbeat_path,
    )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only; pid liveness check")
def test_wall_clock_stale_reaps(fake_wiki: Wiki) -> None:
    """A heartbeat older than ``max_age_s`` is reaped even if the PID is alive.

    Seeds the create-lock + pid file + a heartbeat whose ``started_at``
    is 10h in the past, then calls ``acquire_create_lock`` with
    ``pid_alive_fn=lambda _pid: "alive"``. The PID-alive path yields to
    the wall-clock window, which reaps the stale envelope.
    """
    create_lock = fake_wiki.memory_create_lock_path
    pid_path = fake_wiki.memory_pid_path
    state_json_path = fake_wiki.memory_heartbeat_path

    create_lock.parent.mkdir(parents=True, exist_ok=True)
    fd0 = os.open(str(create_lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    os.close(fd0)
    # Seed a non-current PID so the self-recovery branch in
    # _reap_if_stale() (``stored_pid == os.getpid()``) is bypassed;
    # the wall-clock branch is the only path under test.
    seeded_pid = os.getpid() + 999_999
    write_owner_pid(pid_path, seeded_pid)
    write_heartbeat(
        state_json_path,
        Heartbeat(
            pid=seeded_pid,
            started_at=time.time() - 10 * 3600,
            scope="stale-test",
        ),
    )

    fd_result = acquire_create_lock(
        create_lock,
        max_age_s=7200,
        pid_path=pid_path,
        state_json_path=state_json_path,
        pid_alive_fn=lambda _pid: "alive",
    )
    assert fd_result is not None, "expected wall-clock-stale reap"
    release_create_lock(
        create_lock,
        fd_result.fd,
        pid_path=pid_path,
        state_json_path=state_json_path,
    )


def test_gc_cleanup_on_unexpected_exit(tmp_path: Path, fake_wiki: Wiki) -> None:
    """A child that exits via ``os._exit`` leaves the lock envelope on disk;
    the next ``acquire_create_lock`` reaps it and succeeds.

    ``os._exit`` is a C-level exit that bypasses ``__exit__`` (and
    therefore the ``release_create_lock`` cleanup). Files persist with
    a dead PID; the parent's acquire treats them as stale and reaps.
    """
    proc = _spawn_holder(HOLDER_ACQUIRE_AND_ABORT, tmp_path)
    try:
        _wait_for_ready(proc)
        proc.wait(timeout=5)
    except BaseException:
        _cleanup_holder(proc)
        raise

    fd_result = acquire_create_lock(
        fake_wiki.memory_create_lock_path,
        max_age_s=7200,
        pid_path=fake_wiki.memory_pid_path,
        state_json_path=fake_wiki.memory_heartbeat_path,
    )
    assert fd_result is not None
    release_create_lock(
        fake_wiki.memory_create_lock_path,
        fd_result.fd,
        pid_path=fake_wiki.memory_pid_path,
        state_json_path=fake_wiki.memory_heartbeat_path,
    )
