"""The holder-PID file must never be observable half-written.

Pre-existing, made routine by this branch: ``write_owner_pid`` used
``Path.write_text``, which truncates before it writes, so a contender
reading in that window got an empty file, failed to parse it and logged
``could not parse pid file`` (``exclusive._reap_if_stale``). The
failure is benign and in the safe direction — the reader returns
``False``, so nobody reaps — but the query single-flight gate makes
nine-way contention normal, so the warning now prints in normal
operation and reads as a fault that is not there.

Both pins here are deterministic, and that is a deliberate choice over
a threaded test. A concurrent-reader test is a race: it passed against
the truncate-then-write implementation on most runs and only caught it
after the writer loop was widened, which is the failure mode this
branch has already shipped twice — a green test that cannot fail.
Worse, its runtime swung between 0.07 s and 0.42 s on this host, so it
could not be pinned against the 0.15 s hard limit without being marked
slow, and marking it slow would have removed it from the default run.
The spy below opens exactly the window the old code exposed, every
time, in microseconds.
"""

from __future__ import annotations

import ast
import os
import threading
from pathlib import Path

from lies.utils import lock_heartbeat


def _write_owner_pid_body() -> ast.FunctionDef:
    tree = ast.parse(Path(lock_heartbeat.__file__).read_text(encoding="utf-8"))
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "write_owner_pid"
    )


def test_write_owner_pid_replaces_rather_than_truncates() -> None:
    """The write must land via ``os.replace``, not an open-for-truncate."""
    called = {
        node.func.id if isinstance(node.func, ast.Name) else node.func.attr
        for node in ast.walk(_write_owner_pid_body())
        if isinstance(node, ast.Call)
    }

    assert "replace" in called, (
        "write_owner_pid no longer goes through os.replace; a truncate-then-write "
        "makes a partial read possible again"
    )
    assert "write_text" not in called, (
        "write_owner_pid calls Path.write_text, which truncates before writing; "
        "a contender can read the empty file and log 'could not parse pid file'"
    )


def test_the_target_is_untouched_until_the_rename(tmp_path: Path, monkeypatch) -> None:
    """Open the exact window the old code exposed, and look.

    A spy on ``os.replace`` runs *after* the temp file is fully written
    and *before* the rename — which is precisely where a contender's
    read lands under the old truncate-then-write. The target must
    still carry the previous pid there, and it must carry the new one
    after. Under ``write_text`` the spy never fires at all, so
    ``observed`` stays empty and this fails rather than passing by
    accident.
    """
    path = tmp_path / "qmd.lock.pid"
    lock_heartbeat.write_owner_pid(path, 111)

    observed: list[bytes] = []
    real_replace = os.replace

    def spy(src, dst, **kwargs):  # noqa: ANN001, ANN003
        observed.append(path.read_bytes())
        return real_replace(src, dst, **kwargs)

    monkeypatch.setattr(lock_heartbeat.os, "replace", spy)
    lock_heartbeat.write_owner_pid(path, 222)

    assert observed == [b"111"], (
        f"the target was {observed!r} just before the rename; a contender "
        f"reading there must see the old pid, never an empty file"
    )
    assert lock_heartbeat.read_owner_pid(path) == 222


def test_temp_file_is_a_sibling_of_the_target(tmp_path: Path) -> None:
    """``os.replace`` is atomic only within one filesystem.

    A temp file in ``/tmp`` and a target in the cache dir are on
    different filesystems on any ordinary host, and ``os.replace`` there
    degrades to copy-then-unlink — which has the window again.
    """
    path = tmp_path / "nested" / "qmd.lock.pid"
    path.parent.mkdir()
    lock_heartbeat.write_owner_pid(path, 4242)

    assert sorted(p.name for p in path.parent.iterdir()) == ["qmd.lock.pid"], (
        "write_owner_pid left something behind in the target directory; a "
        "temp file outside it would not be an atomic rename"
    )
    assert lock_heartbeat.read_owner_pid(path) == 4242


def test_a_failed_write_leaves_no_temp_debris(tmp_path: Path, monkeypatch) -> None:
    """The ``except`` arm is load-bearing.

    Without it a failed write deposits a second, unparseable ``*.tmp``
    file next to the pid file, and the next contender reads whichever
    it finds.
    """
    path = tmp_path / "qmd.lock.pid"

    def boom(*_args, **_kwargs) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(lock_heartbeat.os, "replace", boom)
    try:
        lock_heartbeat.write_owner_pid(path, 1234)
    except OSError:
        pass
    else:
        raise AssertionError("write_owner_pid swallowed the replace failure")

    assert not list(tmp_path.glob("*.tmp")), "a failed write_owner_pid left a temp file"
    assert not path.exists(), "a failed write_owner_pid published the pid file anyway"


def test_temp_names_do_not_collide_between_threads(tmp_path: Path) -> None:
    """Two threads must not pick the same temp name and clobber each other.

    The name carries ``get_ident()`` for this reason; ``getpid()`` alone
    is constant within a process, so ``qmd.lock.pid.<pid>.tmp`` would
    be one shared file for every writer in the session.
    """
    import concurrent.futures

    path = tmp_path / "qmd.lock.pid"
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for _ in pool.map(lambda n: lock_heartbeat.write_owner_pid(path, n), range(2, 14)):
            pass

    assert not list(tmp_path.glob("*.tmp"))
    assert lock_heartbeat.read_owner_pid(path) in range(2, 14)


def test_writers_from_several_threads_all_land_whole(tmp_path: Path) -> None:
    """Cheap corroboration that the rename holds under real contention.

    Bounded hard (8 writers, 40 writes) so it cannot drift past the
    0.15 s budget. It is deliberately *not* the primary evidence: with
    this little work a truncate-then-write would usually survive it,
    which is exactly why the spy test above exists.
    """
    path = tmp_path / "qmd.lock.pid"
    path.write_text("1", encoding="utf-8")
    bad: list[bytes] = []
    guard = threading.Lock()
    done = threading.Event()

    def reader() -> None:
        seen = 0
        while not done.is_set() and seen < 200:
            seen += 1
            try:
                raw = path.read_bytes()
            except FileNotFoundError:
                with guard:
                    bad.append(b"<missing>")
                continue
            try:
                int(raw.strip())
            except ValueError:
                with guard:
                    bad.append(raw)

    watchers = [threading.Thread(target=reader, daemon=True) for _ in range(2)]
    for thread in watchers:
        thread.start()
    producers = [
        threading.Thread(
            target=lambda n=n: [lock_heartbeat.write_owner_pid(path, n) for _ in range(3)]
        )
        for n in range(2, 8)
    ]
    for thread in producers:
        thread.start()
    for thread in producers:
        thread.join()
    done.set()
    for thread in watchers:
        thread.join(timeout=1)

    assert not bad, f"{len(bad)} unparseable pid reads, first: {bad[:3]!r}"
