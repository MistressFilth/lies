"""Tests for src/lies/qmd/lock.py — path resolution and decorator signature."""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import inspect
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest


def _lock_module():
    """Return the cached ``lies.qmd.lock`` module.

    The module no longer pre-resolves a lock triad at import
    time (M-6); the production acquire/release pair threads its
    own paths through ``_lock_paths()``, and tests do the same.
    No ``reload()`` is needed because there is nothing frozen at
    the module level any more.
    """
    return importlib.import_module("lies.qmd.lock")


def test_lock_module_imports():
    mod = importlib.import_module("lies.qmd.lock")
    assert mod is not None


def test_lock_path_default_resolves_to_xdg_state_home(monkeypatch):
    """Default path is ``${XDG_STATE_HOME:-~/.local/state}/lies/qmd.lock``.

    With neither ``LIES_QMD_LOCK_PATH`` nor ``XDG_STATE_HOME`` set, the
    resolved lock path falls back to ``~/.local/state/lies/qmd.lock``.

    The lock module no longer pre-creates the lock directory at
    import time (M-6); the path is resolved per acquisition through
    :func:`lies.qmd.lock._lock_paths`, which honours
    ``LIES_QMD_LOCK_PATH`` first, then ``XDG_STATE_HOME``, then
    ``~/.local/state``.
    """
    monkeypatch.delenv("LIES_QMD_LOCK_PATH", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    mod = _lock_module()
    lock_path, _, _ = mod._lock_paths()
    expected = os.path.expanduser("~/.local/state/lies/qmd.lock")
    assert str(lock_path) == expected


def test_lock_path_env_override_takes_precedence(monkeypatch):
    monkeypatch.setenv("LIES_QMD_LOCK_PATH", "/tmp/override-lies-qmd.lock")
    mod = _lock_module()
    lock_path, _, _ = mod._lock_paths()
    assert str(lock_path) == "/tmp/override-lies-qmd.lock"


def test_pid_and_state_paths_share_lock_path_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("LIES_QMD_LOCK_PATH", str(tmp_path / "qmd.lock"))
    mod = _lock_module()
    lock_path, pid_path, state_path = mod._lock_paths()
    assert pid_path.parent == lock_path.parent
    assert state_path.parent == lock_path.parent
    assert pid_path.name.startswith(lock_path.name)
    assert state_path.name.startswith(lock_path.name)


def test_with_qmd_lock_default_signature():
    """Decorator factory exposes default timeout_s=30.0 and max_age_s=1800.0."""
    from lies.qmd.lock import with_qmd_lock

    sig = inspect.signature(with_qmd_lock)
    assert "timeout_s" in sig.parameters
    assert "max_age_s" in sig.parameters
    assert sig.parameters["timeout_s"].default == 30.0
    assert sig.parameters["max_age_s"].default == 1800.0


"""Tests for the qmd flock poll-retry and release paths."""


# A small holder script that acquires the qmd flock and holds it until
# the ready marker is removed. Spawned via subprocess.Popen so the
# contending process has a different pid — required because
# ``acquire_create_lock`` self-recovers when ``stored_pid == os.getpid()``,
# which means in-process contention tests cannot reproduce cross-process
# blocking.
_HOLDER_SCRIPT = textwrap.dedent(
    """\
    import os
    import sys
    import time
    from pathlib import Path

    from lies.qmd.lock import _acquire_with_poll, _release

    ready_marker = Path(sys.argv[1])
    hold_s = float(sys.argv[2])

    fd = _acquire_with_poll(retry_budget_s=60.0, max_age_s=1800.0)
    try:
        ready_marker.write_text("ready", encoding="utf-8")
        time.sleep(hold_s)
    finally:
        _release(fd)
    """
)


def _spawn_qmd_holder(tmp_path: Path, *, hold_s: float) -> tuple[subprocess.Popen, Path]:
    """Spawn a subprocess that holds the qmd flock for ``hold_s`` seconds.

    Returns (process, ready_marker). The caller is responsible for
    terminating ``process`` if the test exits before the holder wakes
    up naturally; otherwise the lock + pid + heartbeat siblings will
    persist into the next test.
    """
    ready_marker = tmp_path / "holder_ready"
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER_SCRIPT, str(ready_marker), str(hold_s)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    _wait_for_marker(ready_marker, timeout=10.0)
    return proc, ready_marker


def _wait_for_marker(marker: Path, *, timeout: float) -> None:
    deadline = time.time() + timeout
    while not marker.exists():
        if time.time() > deadline:
            raise RuntimeError(f"holder did not signal {marker} within {timeout}s")
        if not marker.parent.exists():
            raise RuntimeError(f"marker parent {marker.parent} disappeared")
        time.sleep(0.05)


def _terminate_holder(proc: subprocess.Popen, *, timeout: float = 5.0) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=timeout)


def test_with_qmd_lock_acquires_and_releases_on_clean_path(tmp_path, monkeypatch):
    """Single call against an empty lock dir: acquires, holds during with, releases on exit."""
    import lies.qmd.lock as lock_mod  # type: ignore[import-not-found]

    monkeypatch.setenv("LIES_QMD_LOCK_PATH", str(tmp_path / "qmd.lock"))
    mod = importlib.reload(lock_mod)

    lock_path, pid_path, state_path = mod._lock_paths()
    assert not lock_path.exists()

    @mod.with_qmd_lock()
    def noop() -> str:
        return "ok"

    assert noop() == "ok"
    assert not lock_path.exists()
    assert not pid_path.exists()
    assert not state_path.exists()


@pytest.mark.slow
def test_second_call_blocks_until_first_releases(monkeypatch, tmp_path):
    """Cross-process holder; main-thread acquire blocks until the holder releases."""
    import lies.qmd.lock as lock_mod  # type: ignore[import-not-found]

    monkeypatch.setenv("LIES_QMD_LOCK_PATH", str(tmp_path / "qmd.lock"))
    mod = importlib.reload(lock_mod)

    # Spawn a subprocess holder; the main process is a different pid, so
    # the second acquire busy-polls until the holder releases. The hold
    # window has to be wider than the test runner's subprocess-startup
    # + acquire latency on CI runners (the original 0.05s was tight
    # enough that the holder sometimes released before the waiter
    # could observe the blocked state). The cross-process behavior
    # (busy-poll until holder releases) is what we're exercising, not
    # the wait window.
    holder, _ = _spawn_qmd_holder(tmp_path, hold_s=1.0)
    try:

        @mod.with_qmd_lock(timeout_s=5.0, max_age_s=1800.0)
        def wait_then_acquire() -> str:
            return "second"

        t = threading.Thread(target=wait_then_acquire)
        t.start()
        # Give the waiter a moment to attempt and block.
        time.sleep(0.1)
        assert t.is_alive(), "second call should be blocked while holder holds"
        # Holder releases after ~1.0s; waiter should complete shortly after.
        t.join(timeout=5)
        assert not t.is_alive(), "second call should have completed after holder released"
    finally:
        _terminate_holder(holder)


@pytest.mark.slow
def test_qmd_lock_busy_raises_after_retry_budget(monkeypatch, tmp_path):
    """First call holds forever; second call times out at timeout_s and raises QmdLockBusy."""
    import lies.qmd.lock as lock_mod  # type: ignore[import-not-found]
    from lies.qmd.lock import QmdLockBusy  # type: ignore[import-not-found]
    from lies.lock_errors import WikiFlockError  # type: ignore[import-not-found]

    monkeypatch.setenv("LIES_QMD_LOCK_PATH", str(tmp_path / "qmd.lock"))
    mod = importlib.reload(lock_mod)

    # Spawn a holder subprocess; its pid differs from ours so the second
    # acquire observes a live contender and busy-polls. The holder holds
    # long enough for the busy-poll to exhaust ``timeout_s``; the holder
    # is then terminated by the cleanup path so the test doesn't leak
    # processes into the next one.
    holder, _ = _spawn_qmd_holder(tmp_path, hold_s=2.0)
    try:

        @mod.with_qmd_lock(timeout_s=0.1, max_age_s=1800.0)
        def attempt() -> None:
            return None

        with pytest.raises(QmdLockBusy) as excinfo:
            attempt()
        assert excinfo.value.max_s == pytest.approx(0.1, rel=0.2)
        assert isinstance(excinfo.value, WikiFlockError)
    finally:
        _terminate_holder(holder)


@pytest.mark.slow
def test_holder_pid_in_qmd_lock_busy_when_holder_writes_heartbeat(monkeypatch, tmp_path):
    """Holder-acquire path writes pid + heartbeat. LockBusy surfaces that pid."""
    import lies.qmd.lock as lock_mod  # type: ignore[import-not-found]

    monkeypatch.setenv("LIES_QMD_LOCK_PATH", str(tmp_path / "qmd.lock"))
    mod = importlib.reload(lock_mod)

    holder, _ = _spawn_qmd_holder(tmp_path, hold_s=2.0)
    lock_path, pid_path, _ = mod._lock_paths()
    try:
        # Confirm the holder's pid was registered to the pid file before
        # we attempt the contended acquire; this verifies the write path
        # is engaged by ``_acquire_with_poll`` (not just the decorator).
        _wait_for_marker(pid_path, timeout=5.0)
        holder_pid = int(pid_path.read_text(encoding="utf-8").strip())
        assert holder_pid != os.getpid(), "holder pid should differ from the test pid"

        @mod.with_qmd_lock(timeout_s=0.1, max_age_s=1800.0)
        def attempt() -> None:
            return None

        with pytest.raises(mod.QmdLockBusy) as excinfo:
            attempt()
        assert excinfo.value.holder_pid == holder_pid
    finally:
        _terminate_holder(holder)


def test_stale_holder_recovery_via_dead_pid(monkeypatch, tmp_path):
    """A lock file whose stored pid is dead gets reaped on the next acquire."""
    import lies.qmd.lock as lock_mod  # type: ignore[import-not-found]

    monkeypatch.setenv("LIES_QMD_LOCK_PATH", str(tmp_path / "qmd.lock"))
    mod = importlib.reload(lock_mod)

    # Manually stage: create-lock + pid file pointing at a dead pid.
    lock_path, pid_path, _ = mod._lock_paths()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.touch()
    pid_path.write_text("999999", encoding="utf-8")  # likely-dead pid

    # Acquire should reap and succeed.
    fd = mod._acquire_with_poll(retry_budget_s=1.0, max_age_s=60.0)
    try:
        assert fd >= 0
    finally:
        mod._release(fd)


def _stage_a_live_holder(lock_mod, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Stage a lock held by a *live* process -- this one.

    Same-pid contention is what a second thread or task really looks like,
    and ``self_acquire_is_stale=False`` is what keeps it contention rather
    than self-recovery. Staging it by hand avoids a subprocess holder: the
    reaping and escalation paths are covered above, and what is under test
    here is what the poll loop does while losing, which a subprocess would
    only make slower to arrange.
    """
    monkeypatch.setenv("LIES_QMD_LOCK_PATH", str(tmp_path / "qmd.lock"))
    lock_path, pid_path, _ = lock_mod._lock_paths()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.touch()
    pid_path.write_text(str(os.getpid()), encoding="utf-8")
    return lock_path


def _count_lock_path_resolutions(lock_mod, monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Wrap ``_lock_paths`` with a counter. Returns the call-count list."""
    real = lock_mod._lock_paths
    calls = [0]

    def counting() -> tuple[Path, Path, Path]:
        calls[0] += 1
        return real()

    monkeypatch.setattr(lock_mod, "_lock_paths", counting)
    return calls


def test_a_contended_sync_acquire_resolves_the_lock_path_once(monkeypatch, tmp_path: Path) -> None:
    """One resolution per *acquisition*, however many attempts it takes.

    ``_lock_paths`` is not free: ``Path.resolve()`` walks the filesystem to
    settle symlinks, and on the ``XDG_STATE_HOME`` fallback branch it
    ``mkdir(parents=True)`` first. Resolving per attempt does that at the
    poll rate — ten times a second, for the whole wait — on a path that
    cannot change mid-acquire. The sync loop has always resolved once, and
    the poll interval is short enough to multiply the waste into something
    measurable on a contended host.

    The poll interval is shrunk so the contended acquire takes many attempts
    cheaply; that is what gives the assertion teeth. At one resolution per
    attempt this would be double digits.
    """
    import lies.qmd.lock as lock_mod  # type: ignore[import-not-found]

    _stage_a_live_holder(lock_mod, tmp_path, monkeypatch)
    monkeypatch.setattr(lock_mod, "_POLL_INTERVAL_S", 0.005)
    calls = _count_lock_path_resolutions(lock_mod, monkeypatch)

    with pytest.raises(lock_mod.QmdLockBusy):
        lock_mod._acquire_with_poll(retry_budget_s=0.05, max_age_s=1800.0)

    assert calls[0] == 1, (
        f"a contended sync acquire resolved the lock triad {calls[0]}x; it "
        "must resolve once before the loop, not once per attempt"
    )


async def test_a_contended_async_acquire_resolves_the_lock_path_once(
    monkeypatch, tmp_path: Path
) -> None:
    """The async twin of the above, for the same reason.

    Nothing in production preserved per-attempt resolution here, so this is
    a choice rather than a regression guard: the async poll runs at the same
    rate against the same helper, and a wait that can be 120s would do the
    walk 1200 times for nothing.
    """
    import lies.qmd.lock as lock_mod  # type: ignore[import-not-found]

    _stage_a_live_holder(lock_mod, tmp_path, monkeypatch)
    monkeypatch.setattr(lock_mod, "_POLL_INTERVAL_S", 0.005)
    calls = _count_lock_path_resolutions(lock_mod, monkeypatch)

    with pytest.raises(lock_mod.QmdLockBusy):
        await lock_mod._acquire_with_poll_async(retry_budget_s=0.05, max_age_s=1800.0)

    assert calls[0] == 1, (
        f"a contended async acquire resolved the lock triad {calls[0]}x; it "
        "must resolve once before the loop, not once per attempt"
    )


"""Meta-tests: every qmd_* helper in src/lies/qmd/cli.py is wrapped."""


def test_every_qmd_helper_is_with_qmd_lock_wrapped():
    from lies.qmd import cli as qmd_cli

    qmd_callables = [
        (name, obj)
        for name, obj in vars(qmd_cli).items()
        if name.startswith("qmd_")
        and callable(obj)
        and name != "is_qmd_installed"  # doesn't shell out
    ]

    # Whitelist exception: helpers that legitimately don't shell out
    # and don't need the flock. Today only is_qmd_installed.
    expected_wrapped = {name for name, _ in qmd_callables if name != "is_qmd_installed"}

    missing: list[str] = []
    for name, obj in qmd_callables:
        if name not in expected_wrapped:
            continue
        wrapped = getattr(obj, "__wrapped__", None)
        if wrapped is None:
            missing.append(name)
    assert not missing, f"qmd helpers missing @with_qmd_lock: {missing}"


def test_is_qmd_installed_does_not_have_lock_wrapper():
    """``is_qmd_installed`` is a stub probe with no subprocess; no flock needed."""
    from lies.qmd.cli import is_qmd_installed

    assert getattr(is_qmd_installed, "__wrapped__", None) is None


# --- the lock must survive test isolation --------------------------------
#
# `with_qmd_lock` derives its path from `XDG_STATE_HOME`. The autouse
# `_isolated_xdg` fixture redirects that per test, which gave every test
# its own inode -- and a per-test lock is no lock at all. Two embeds then
# raced the CUDA VMM pool reservation the lock exists to serialize, and the
# symptom was `CUDA error: out of memory` from `ggml-cuda.cu`.


def _flock_path_used_by_the_decorator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The file ``with_qmd_lock`` actually opens, observed rather than computed.

    An earlier version of these tests called ``_lock_paths()`` directly,
    which certifies a guarantee the suite did not have: the acquire path
    never called the resolver, it used a constant frozen at import, so
    ``_isolated_xdg``\'s pin landed after the freeze and every test flocked
    the *host default* instead. Asserting on the resolver therefore tested
    a function the decorator does not use.

    This one intercepts the real call: acquire with a sentinel
    ``LIES_QMD_LOCK_PATH`` and read back which file appeared. That
    catches a regression back to import-time freezing, because the
    sentinel set after import would be ignored and the host default would
    be opened instead.
    """
    # Import first, then set the environment. That order is the whole
    # point: the module constant is resolved at import, so a sentinel set
    # beforehand would be baked into it and the acquire path would look
    # correct whether it used the constant or the resolver. Setting it
    # afterwards is what distinguishes them.
    import lies.qmd.lock as lock_mod

    sentinel = tmp_path / "observed-qmd.lock"
    monkeypatch.setenv("LIES_QMD_LOCK_PATH", str(sentinel))

    with_qmd_lock = lock_mod.with_qmd_lock

    # Observed *during* the hold, not after: the release path unlinks the
    # lock file, so checking afterwards would pass whether or not the
    # decorator ever opened it.
    seen: dict[str, bool] = {}

    @with_qmd_lock(timeout_s=5.0)
    def _hold() -> None:
        seen["lock"] = sentinel.exists()

    _hold()
    resolved_path, _, _ = lock_mod._lock_paths()
    assert seen.get("lock"), (
        f"the decorator never opened {sentinel} while holding the lock. It is "
        f"resolving from a stale path ({resolved_path}) instead of the "
        f"sentinel set after import."
    )
    assert resolved_path == sentinel, (
        f"_lock_paths() must honour LIES_QMD_LOCK_PATH at call time; got {resolved_path}"
    )
    return sentinel


def test_the_decorator_uses_the_pinned_lock_not_the_import_time_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pin must reach the flock, not just the resolver.

    This is the defect the previous round shipped: the pin was real, the
    tests passed, and the lock still went to
    ``~/.local/state/lies/qmd.lock`` because the acquire path read a
    constant frozen at import. If resolution regresses to import-time,
    the sentinel below is ignored and the host default is opened --
    which the ``host default not used`` assertion catches.
    """
    from lies.qmd.lock import _lock_paths

    sentinel = _flock_path_used_by_the_decorator(tmp_path, monkeypatch)

    # The file the decorator touched is the one the environment names.
    assert _lock_paths()[0] == sentinel, (
        f"the decorator opened {_lock_paths()[0]}, not the pinned {sentinel}"
    )
    # And it is not the host default that the frozen constant still holds.
    assert _lock_paths()[0] != Path("~/.local/state/lies/qmd.lock").expanduser(), (
        "the decorator used the import-time host default; resolution is frozen again"
    )


def test_acquire_after_import_honours_a_later_env_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Changing the environment *after* import must still take effect.

    This is what ``_lock_paths`` has always claimed ("Resolved on every
    call so environment changes between acquisitions are honored") and
    what the code now does. The claim outlived the behaviour for the life
    of the module, so it is pinned here directly.
    """
    import lies.qmd.lock as lock_mod

    first = _flock_path_used_by_the_decorator(tmp_path / "a", monkeypatch)
    second = _flock_path_used_by_the_decorator(tmp_path / "b", monkeypatch)

    assert first != second, (
        "two different LIES_QMD_LOCK_PATH values produced the same lock; "
        "resolution is frozen at import again"
    )
    assert lock_mod._lock_paths()[0] == second


def test_the_shared_qmd_lock_is_outside_the_per_test_xdg_root(tmp_path: Path) -> None:
    """The pinned lock must not live under the per-test XDG root.

    The subtle failure: putting the lock at ``xdg_root / "state"`` reads
    as tidy isolation while reproducing the original bug -- one inode per
    test is exactly what there must not be. This pins the shape of the fix,
    not merely its presence.
    """
    from lies.qmd.lock import _lock_paths

    lock = _lock_paths()[0]

    assert not str(lock).startswith(str(tmp_path)), (
        f"the qmd lock resolved inside this test's tmp_path ({lock}); that is a "
        f"per-test lock, which excludes nothing"
    )
    assert _lock_paths()[0] == lock, "and it must be stable across calls within one test"


def test_the_pinned_test_lock_is_per_user_and_per_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shared test lock must be keyed by uid *and* by session.

    ``gettempdir()`` is shared on a multi-account CI host, so the uid
    keeps two accounts off one file. The pid keeps two *pytest
    processes* off one file as well: a uid-only key made every pytest
    process owned by this user contend, so running the unit suite while
    any other ``lies`` work was live produced six 30-second hangs and
    six ``QmdLockBusy`` failures that read as product bugs.

    Intra-session sharing is the property that actually matters — one
    process, one path, every test in it — and the pid preserves it.
    """
    from lies.qmd.lock import _lock_paths

    # Read the expression out of the fixture rather than restating it, so
    # a change to the pin is visible here instead of silently diverging.
    root_conftest = Path(__file__).resolve().parents[2] / "conftest.py"
    source = root_conftest.read_text(encoding="utf-8")
    assert "lies-test-qmd-" in source, "the shared test lock pin is gone from conftest"
    assert "os.getuid()" in source, (
        "the pinned test lock must carry the uid, or two accounts on a "
        "shared CI host contend on one file"
    )
    assert "os.getpid()" in source, (
        "the pinned test lock must carry the pid, or two pytest sessions "
        "on this user contend and hang for 30s each"
    )

    lock = _lock_paths()[0]
    assert f"{os.getuid()}" in lock.name, (
        f"the pinned test lock {lock.name} carries no uid; two accounts on a "
        f"shared host would contend on it"
    )
    assert f"{os.getpid()}" in lock.name, (
        f"the pinned test lock {lock.name} carries no pid; two pytest "
        f"sessions on this user would contend on it"
    )
    assert str(os.getuid()) in lock.name


def test_embed_is_lock_wrapped() -> None:
    """The exclusion this depends on has to actually cover ``qmd_embed``.

    The fixture is only safe because every qmd helper goes through
    ``with_qmd_lock``. A future helper that shells out without it joins
    the CUDA reservation race, and nothing else in the suite would notice.
    """
    from lies.qmd import cli

    # One condition, stated once. The previous version was a disjunction
    # of a condition with itself, whose `hasattr` arm also passed when
    # the attribute existed with value None.
    assert hasattr(cli.qmd_embed, "__wrapped__"), "qmd_embed must still be wrapped by with_qmd_lock"
    assert cli.qmd_embed.__wrapped__ is not None, (
        "qmd_embed is wrapped but the wrapper is None, so the lock is not actually applied"
    )


def test_the_lock_is_reentrant_on_one_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A nested acquire in the same context must not deadlock against itself.

    One pair of decorated helpers composes: ``qmd_collection_add_or_update``
    calls ``qmd_collection_show`` and ``qmd_collection_add``. ``flock`` on
    a second fd blocks even within one process, so without reentrancy the
    inner call polls for a lock its own outer frame holds and times out with
    ``QmdLockBusy``.

    (``qmd_cleanup`` and ``qmd_reindex(cleanup=True)`` are *not* a second
    example, though an earlier version of this docstring said they were:
    both call ``_proc.run(["cleanup"], ...)`` directly instead of going
    through each other, so neither nests.)

    This never fired while the acquire path used the import-time constant:
    the inner acquire opened a *different* file, so it never contended and
    the nesting was invisible. Per-acquisition resolution exposed it, and
    this pins the fix.

    The nesting is same-context composition, not concurrency -- a genuine
    second thread must still serialize, which
    ``test_a_second_thread_still_contends`` covers, and a raise through
    the nested frame must not strand the depth, which
    ``test_a_raising_nested_frame_restores_the_depth`` covers.
    """
    from lies.qmd.lock import with_qmd_lock

    monkeypatch.setenv("LIES_QMD_LOCK_PATH", str(tmp_path / "reentrant.lock"))
    order: list[str] = []

    @with_qmd_lock(timeout_s=5.0)
    def inner() -> None:
        order.append("inner")

    @with_qmd_lock(timeout_s=5.0)
    def outer() -> None:
        order.append("outer-enter")
        inner()
        order.append("outer-exit")

    outer()  # must not raise QmdLockBusy

    assert order == ["outer-enter", "inner", "outer-exit"]


def test_a_raising_nested_frame_restores_the_depth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raise through the nested frame must not strand the depth counter.

    Reentrancy is bookkeeping: the outer frame bumps the depth, the inner
    frame runs without acquiring, and both restore on the way out. If a
    raise skipped the restore on either frame, the depth would be left
    above zero for the rest of the process -- and because the very next
    acquire consults that depth, **every** subsequent call in that process
    would skip the flock. That is a silent, permanent loss of the mutual
    exclusion the lock exists to provide, and it would not raise anywhere:
    it would just stop protecting.

    Asserts on the counter rather than on observed contention. A
    contention-shaped version needs a thread to hold the lock while this
    one blocks on it, which costs more than the 0.15s unit budget
    (`test_a_second_thread_still_contends` measures 0.214s and lives in
    tests/integration for that reason). The stranded-depth failure *is*
    "the counter is not 0", so read it directly rather than inferring it
    through a timing window.
    """
    from lies.qmd.lock import _held_depth, with_qmd_lock

    monkeypatch.setenv("LIES_QMD_LOCK_PATH", str(tmp_path / "raise-depth.lock"))

    @with_qmd_lock(timeout_s=5.0)
    def raises() -> None:
        raise ValueError("boom")

    @with_qmd_lock(timeout_s=5.0)
    def outer() -> None:
        raises()

    with pytest.raises(ValueError, match="boom"):
        outer()

    assert _held_depth.get() == 0, (
        "the depth was left above zero by a raise, so every later acquire "
        "in this process will skip the flock"
    )

    # And the counter is still usable: a normal call after the raise must
    # take the outermost path (depth goes 0 -> 1 -> 0), not the reentrant
    # one. If it took the reentrant path the file would never be opened.
    seen: list[int] = []

    @with_qmd_lock(timeout_s=5.0)
    def probe() -> None:
        seen.append(_held_depth.get())

    probe()
    assert seen == [1], (
        f"a post-raise call ran at depth {seen!r} rather than 1, so it took "
        "the reentrant path and never acquired the flock"
    )
    assert _held_depth.get() == 0


# --- the async acquire -------------------------------------------------------
#
# The MCP daemon's query path is async. `with_qmd_lock` polls with
# `time.sleep`, so reusing it there would block the whole event loop for the
# length of the wait -- inside the daemon that freezes every concurrent
# request, including the ones that never touch qmd. That is worse than the
# unbounded queue this primitive exists to bound, so the async context manager
# awaits between polls instead.
#
# Two properties of the harness make the contention tests below non-obvious,
# and both were verified against this codebase rather than assumed:
#
# 1. The lock path comes from `LIES_QMD_LOCK_PATH`, which `tests/conftest.py`
#    pins to one file per session so two tests in a run still exclude each
#    other. Patching `_default_lock_dir` redirects nothing: `_lock_paths()`
#    consults the env var first and never reaches the fallback. These tests
#    move the pin instead, into their own `tmp_path`.
# 2. `_held_depth` is a `ContextVar`, and an `@contextlib.asynccontextmanager`
#    body runs in its caller's context -- the `set()` inside it is visible to
#    the caller for the duration of the `async with`. So a nested acquire in the
#    same task is *reentrant* and never contends; it would sail through and
#    never raise. Genuine contention is a second **task**, which snapshots the
#    context at creation. Every contender below is therefore created *before*
#    the outer acquire, so its snapshot carries depth 0 -- which is also the
#    production shape, since two concurrent MCP requests are two tasks.


def test_query_lock_wait_defaults_to_120(monkeypatch: pytest.MonkeyPatch) -> None:
    from lies.qmd import lock as lock_mod

    monkeypatch.delenv("LIES_QMD_QUERY_LOCK_WAIT", raising=False)
    assert lock_mod.query_lock_wait_s() == 120.0


def test_query_lock_wait_reads_override(monkeypatch: pytest.MonkeyPatch) -> None:
    from lies.qmd import lock as lock_mod

    monkeypatch.setenv("LIES_QMD_QUERY_LOCK_WAIT", "45")
    assert lock_mod.query_lock_wait_s() == 45.0


def test_query_lock_wait_survives_malformed_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bad env var must not take retrieval down.

    The knob is read on the daemon's query path. Raising `ValueError` from
    here would turn a typo in a shell profile into "every query fails", which
    is a far worse failure than ignoring the knob.
    """
    from lies.qmd import lock as lock_mod

    monkeypatch.setenv("LIES_QMD_QUERY_LOCK_WAIT", "not-a-number")
    assert lock_mod.query_lock_wait_s() == 120.0


def test_query_lock_wait_rejects_a_non_positive_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero or negative would mean "never wait", which is not what a timeout means.

    Fall back to the default rather than honouring it: a wait budget that
    fires instantly turns ordinary transient contention into a user-visible
    failure on every concurrent request.
    """
    from lies.qmd import lock as lock_mod

    monkeypatch.setenv("LIES_QMD_QUERY_LOCK_WAIT", "0")
    assert lock_mod.query_lock_wait_s() == 120.0
    monkeypatch.setenv("LIES_QMD_QUERY_LOCK_WAIT", "-30")
    assert lock_mod.query_lock_wait_s() == 120.0


def test_query_lock_wait_rejects_a_non_finite_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """`inf` parses and passes `> 0`, so the guard above it does not catch it.

    Accepted, it turns the poll into an unbounded wait: a contended query
    retries forever with no diagnostic anywhere, which is the "no bound and
    no visibility" failure this primitive exists to remove — reintroduced
    through the configuration meant to bound it. `nan` compares false
    against `> 0` already; it is pinned here so the reason it falls back is
    recorded rather than incidental.
    """
    from lies.qmd import lock as lock_mod

    for raw in ("inf", "+inf", "Infinity", "nan", "-inf"):
        monkeypatch.setenv("LIES_QMD_QUERY_LOCK_WAIT", raw)
        assert lock_mod.query_lock_wait_s() == 120.0, f"{raw!r} was accepted as a wait budget"


def test_qmd_lock_async_default_signature() -> None:
    """The daemon query path gets a 120s budget, not the CLI's 30s.

    A query is dispatched to a daemon that is already serving other work and
    answers in seconds; the 30s CLI budget was sized for a subprocess that
    has not started yet. The signature is introspected rather than called so
    this pins the declared default, which is what a call site that omits the
    argument gets.
    """
    from lies.qmd import lock as lock_mod

    sig = inspect.signature(lock_mod.qmd_lock_async)
    assert sig.parameters["timeout_s"].default == 120.0
    assert sig.parameters["max_age_s"].default == 1800.0
    # And the CLI budget is untouched by any of this.
    assert inspect.signature(lock_mod.with_qmd_lock).parameters["timeout_s"].default == 30.0


async def test_qmd_lock_async_acquires_and_releases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clean path: acquires, yields, and leaves no lock file behind."""
    from lies.qmd import lock as lock_mod

    monkeypatch.setenv("LIES_QMD_LOCK_PATH", str(tmp_path / "qmd.lock"))
    lock_path, pid_path, state_path = lock_mod._lock_paths()

    async with lock_mod.qmd_lock_async(timeout_s=1.0) as held:
        assert held is None
        assert lock_path.exists(), "the acquire never opened the lock file"

    assert not lock_path.exists()
    assert not pid_path.exists()
    assert not state_path.exists()
    assert lock_mod._held_depth.get() == 0


async def test_qmd_lock_async_raises_when_budget_exhausted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second *task* contending for a held lock gets `QmdLockBusy`, naming the holder."""
    from lies.qmd import lock as lock_mod

    monkeypatch.setenv("LIES_QMD_LOCK_PATH", str(tmp_path / "qmd.lock"))

    async def contender() -> None:
        async with lock_mod.qmd_lock_async(timeout_s=0.05):
            pytest.fail("a second task acquired a lock this test is holding")

    # Created before the outer acquire: a task snapshots the context at
    # creation, so this one carries depth 0 and contends for the real inode.
    # Created afterwards it would snapshot depth 1 and take the reentrant
    # path, which returns without touching the lock and never raises.
    task = asyncio.create_task(contender())

    async with lock_mod.qmd_lock_async(timeout_s=5.0):
        with pytest.raises(lock_mod.QmdLockBusy) as excinfo:
            await task

    assert excinfo.value.max_s == pytest.approx(0.05)
    assert excinfo.value.waited_s is not None
    assert excinfo.value.waited_s >= 0.0
    # Same process, so the holder is us; `self_acquire_is_stale=False` keeps
    # the contender from reaping the lock its own process holds.
    assert excinfo.value.holder_pid == os.getpid()


async def test_the_async_sleep_is_clamped_to_the_remaining_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``min(_POLL_INTERVAL_S, remaining)`` -- the clamp the docstring claims.

    ``_acquire_with_poll_async`` is the only new algorithm this branch
    introduced, and deleting the ``min(...)`` survives every other test
    in this file and in ``test_access.py``. This is the only thing pinning
    it, and the docstring makes a specific falsifiable claim about it: a
    flat ``_POLL_INTERVAL_S`` "overshoots a short budget by up to a full
    poll interval -- a 50 ms wait costs 100 ms".

    The poll interval is **raised** to 2 s here rather than left at its
    100 ms production default. That inverts the usual instinct and is
    what makes the assertion robust rather than twitchy: with the clamp
    the sleep is ``min(2.0, ~0.02)`` = 0.02 s, so the contention resolves
    in ~20 ms and the ceiling can sit far from both candidate numbers;
    without it the sleep is a flat 2.0 s. A 0.5 s ceiling therefore
    clears comfortably on a loaded host and still fails the mutation
    outright -- the alternative (asserting ``waited_s`` is merely under
    the 0.1 s production interval) would be a 2 ms margin over a
    contended acquire and would flake.

    The contender task is created *before* the outer acquire so it
    snapshots a depth-0 context; created afterwards it would take
    ``qmd_lock_async``'s reentrant branch and return without contending.
    """
    from lies.qmd import lock as lock_mod

    monkeypatch.setenv("LIES_QMD_LOCK_PATH", str(tmp_path / "qmd.lock"))
    monkeypatch.setattr(lock_mod, "_POLL_INTERVAL_S", 2.0)

    async def contender() -> None:
        async with lock_mod.qmd_lock_async(timeout_s=0.02):
            pytest.fail("a second task acquired a lock this test is holding")

    task = asyncio.create_task(contender())

    async with lock_mod.qmd_lock_async(timeout_s=5.0):
        with pytest.raises(lock_mod.QmdLockBusy) as excinfo:
            await task

    waited = excinfo.value.waited_s
    assert waited is not None
    assert waited < 0.5, (
        f"the wait was not clamped to the remaining budget: waited {waited:.3f}s "
        f"on a 0.02s budget with a 2.0s poll interval (an unclamped sleep "
        f"costs a full 2.0s)"
    )


async def test_the_busy_message_names_the_lock_path_and_the_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exhausted-budget raise names the inode it waited on.

    Spec §Testing item 2: the message must name *both* the lock path and
    the budget. The path is not decoration — `LIES_QMD_LOCK_PATH`
    overrides it, so the message is the only record of which inode was
    contended, and a reader who queued for 120s and gave up needs to be
    able to go and look at it.

    The triad is resolved once per acquisition, so the raise site already
    holds the path; this asserts it reaches the string a human reads, not
    just an attribute on the exception. `test_lock_errors.py` covers the
    constructor on its own; this is the seam from poll loop to message.
    """
    from lies.qmd import lock as lock_mod

    monkeypatch.setenv("LIES_QMD_LOCK_PATH", str(tmp_path / "qmd.lock"))
    lock_path, _, _ = lock_mod._lock_paths()

    async def contender() -> None:
        async with lock_mod.qmd_lock_async(timeout_s=0.05):
            pytest.fail("a second task acquired a lock this test is holding")

    task = asyncio.create_task(contender())  # depth-0 snapshot; see above

    async with lock_mod.qmd_lock_async(timeout_s=5.0):
        with pytest.raises(lock_mod.QmdLockBusy) as excinfo:
            await task

    message = str(excinfo.value)
    assert str(lock_path) in message, (
        f"the exhausted-budget raise must name the contended inode; got {message!r}"
    )
    assert "0.05" in message, f"the raise must name the budget it waited; got {message!r}"


async def test_qmd_lock_async_does_not_block_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent heartbeat must keep ticking while a contended acquire waits.

    This is the whole reason the async context manager exists. `time.sleep` in
    the poll loop would freeze the loop, so the heartbeat would never be
    scheduled and the counter would stay at zero -- which is exactly the
    failure mode inside the MCP daemon: one slow request stops every other
    request in the process, including ones that never reach qmd.
    """
    from lies.qmd import lock as lock_mod

    monkeypatch.setenv("LIES_QMD_LOCK_PATH", str(tmp_path / "qmd.lock"))
    ticks = 0

    async def heartbeat() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.005)
            ticks += 1

    async def contender() -> None:
        async with lock_mod.qmd_lock_async(timeout_s=0.06):
            pytest.fail("a second task acquired a lock this test is holding")

    beat = asyncio.create_task(heartbeat())
    task = asyncio.create_task(contender())  # depth-0 snapshot; see above
    try:
        with pytest.raises(lock_mod.QmdLockBusy):
            async with lock_mod.qmd_lock_async(timeout_s=5.0):
                await task
    finally:
        beat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await beat

    # ~12 ticks at 5ms over a 60ms wait. The floor is deliberately loose:
    # the claim under test is "the loop ran at all", not a tick count.
    assert ticks >= 3, (
        f"the event loop was blocked while waiting for the lock (heartbeat ticked {ticks}x)"
    )


async def test_qmd_lock_async_releases_on_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed caller must not strand the lock.

    The next request would then poll against a lock nobody holds and burn its
    whole budget before failing -- a self-inflicted denial of service with no
    live process to blame.
    """
    from lies.qmd import lock as lock_mod

    monkeypatch.setenv("LIES_QMD_LOCK_PATH", str(tmp_path / "qmd.lock"))
    lock_path, pid_path, state_path = lock_mod._lock_paths()

    with pytest.raises(RuntimeError, match="boom"):
        async with lock_mod.qmd_lock_async(timeout_s=1.0):
            assert lock_path.exists()
            raise RuntimeError("boom")

    assert not lock_path.exists()
    assert not pid_path.exists()
    assert not state_path.exists()
    assert lock_mod._held_depth.get() == 0

    # And the next caller gets it immediately rather than polling a stray file.
    async with lock_mod.qmd_lock_async(timeout_s=0.05):
        pass


async def test_qmd_lock_async_releases_on_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An abandoned tool call is cancelled, not exceptioned.

    `asyncio.CancelledError` is a `BaseException`, so a release wired only to
    `except Exception` -- or to an `except` clause that does not name it --
    would skip the cleanup. The next caller is then blocked for the remainder
    of its budget by a lock held by a request that no longer exists.

    The cancellation is a real ``task.cancel()``, and that detail is the
    test. ``asyncio.timeout`` -- the obvious way to write this -- *converts*
    the CancelledError into a TimeoutError before it reaches the context
    manager, so the generator unwinds on the exception path and the test
    proves only what `test_qmd_lock_async_releases_on_exception` already
    proves. An implementation whose release hangs off ``except Exception``
    passes such a test and strands the lock on every real cancellation.

    So the exception delivered at the generator's ``yield`` is observed
    rather than assumed: `recording` wraps the real context manager and
    records what unwound it, in the same task and therefore the same
    ``_held_depth`` context.
    """
    from lies.qmd import lock as lock_mod

    monkeypatch.setenv("LIES_QMD_LOCK_PATH", str(tmp_path / "qmd.lock"))
    lock_path, pid_path, state_path = lock_mod._lock_paths()
    seen: list[str] = []
    depth_after: list[int] = []

    @contextlib.asynccontextmanager
    async def recording(inner):
        try:
            async with inner:
                yield
        except BaseException as exc:  # noqa: BLE001 — the point is to catch everything
            seen.append(type(exc).__name__)
            # Read in this task's context, after the inner generator has
            # unwound: a stranded depth is invisible from the test body,
            # because the holder ran in its own context.
            depth_after.append(lock_mod._held_depth.get())
            raise

    async def holder() -> None:
        async with recording(lock_mod.qmd_lock_async(timeout_s=1.0)):
            await asyncio.sleep(30.0)  # cancelled from outside

    task = asyncio.create_task(holder())
    try:
        # The holder acquires synchronously, so yielding once is enough for
        # it to be inside the critical section. Assert rather than assume:
        # cancelling before the acquire would test nothing.
        await asyncio.sleep(0)
        assert lock_path.exists(), "the holder never acquired the lock"
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()

    assert seen == ["CancelledError"], (
        f"the context manager unwound on {seen!r}, not on CancelledError — "
        "this test is not exercising the cancellation path at all"
    )
    assert depth_after == [0], (
        f"_held_depth was left at {depth_after!r} by the cancellation; every "
        "later acquire in that context will skip the flock"
    )
    assert not lock_path.exists(), "cancellation stranded the lock file"
    assert not pid_path.exists()
    assert not state_path.exists()

    # And the next caller gets it immediately rather than polling a stray
    # file, both here and in a fresh context.
    async with lock_mod.qmd_lock_async(timeout_s=0.05):
        pass


async def test_qmd_lock_async_is_reentrant_within_one_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A nested acquire in the same task must not deadlock against itself.

    The async analogue of `test_the_lock_is_reentrant_on_one_thread`, and it
    matters more here: a query path that composes two locked helpers nests the
    way the CLI helpers do, and the inner acquire would otherwise poll for a
    lock its own outer frame is holding. The inner budget is deliberately tiny
    -- an implementation that actually acquires would raise rather than hang.
    """
    from lies.qmd import lock as lock_mod

    monkeypatch.setenv("LIES_QMD_LOCK_PATH", str(tmp_path / "qmd.lock"))
    order: list[str] = []

    async with lock_mod.qmd_lock_async(timeout_s=5.0):
        order.append("outer-enter")
        async with lock_mod.qmd_lock_async(timeout_s=0.05):
            order.append("inner")
        order.append("outer-exit")

    assert order == ["outer-enter", "inner", "outer-exit"]
    assert lock_mod._held_depth.get() == 0


async def test_qmd_lock_async_restores_the_depth_after_a_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raise through a nested frame must not strand the depth counter.

    Reentrancy is bookkeeping, and a stranded counter fails silently: every
    later acquire in that context sees depth > 0, skips the flock, and the
    mutual exclusion is simply gone with nothing raised anywhere. Asserted on
    the counter, as `test_a_raising_nested_frame_restores_the_depth` is --
    the stranded state *is* "the counter is not 0".
    """
    from lies.qmd import lock as lock_mod

    monkeypatch.setenv("LIES_QMD_LOCK_PATH", str(tmp_path / "qmd.lock"))

    with pytest.raises(ValueError, match="boom"):
        async with lock_mod.qmd_lock_async(timeout_s=5.0):
            async with lock_mod.qmd_lock_async(timeout_s=0.05):
                raise ValueError("boom")

    assert lock_mod._held_depth.get() == 0, (
        "the depth was left above zero by a raise, so every later acquire in "
        "this context will skip the flock"
    )

    seen: list[int] = []

    async def probe() -> None:
        seen.append(lock_mod._held_depth.get())

    async with lock_mod.qmd_lock_async(timeout_s=0.05):
        await probe()

    assert seen == [1], (
        f"a post-raise acquire ran at depth {seen!r} rather than 1, so it took "
        "the reentrant path and never acquired the flock"
    )


async def test_a_second_task_still_contends_under_the_async_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The depth counter must not leak across tasks, or the gate is a no-op.

    Two concurrent MCP requests are two tasks, and that pair is exactly what
    the single-flight is for. If the depth set by one task were visible to the
    other, both would enter the critical section at once and the lock would
    exclude nothing -- with no error raised anywhere.

    ``_POLL_INTERVAL_S`` is shortened to 10 ms for this test. The waiter's
    first attempt lands at 10 ms while the holder still has 40 ms to go, so it
    must poll; at the production 100 ms interval the waiter's *second* attempt
    is at 110 ms and the whole test costs a full poll interval for one sleep
    -- 0.12 s against the repo's 0.15 s hard limit, and systematic, so the
    budget gate's min-of-3 re-measure could not clear it. Shortening the
    interval leaves the holder's 50 ms hold and the contention window
    untouched; it only makes the waiter notice the release sooner. This is
    the real flock and the real gate, only sampled faster.
    """
    from lies.qmd import lock as lock_mod

    monkeypatch.setenv("LIES_QMD_LOCK_PATH", str(tmp_path / "qmd.lock"))
    monkeypatch.setattr(lock_mod, "_POLL_INTERVAL_S", 0.01)
    order: list[str] = []

    async def holder() -> None:
        async with lock_mod.qmd_lock_async(timeout_s=5.0):
            order.append("holder-enter")
            await asyncio.sleep(0.05)
            order.append("holder-exit")

    async def waiter() -> None:
        # A fresh task starting at depth 0 has to wait for the holder's
        # release; it cannot shortcut through the reentrant path.
        await asyncio.sleep(0.01)
        async with lock_mod.qmd_lock_async(timeout_s=5.0):
            order.append("waiter-enter")

    await asyncio.gather(holder(), waiter())

    assert order == ["holder-enter", "holder-exit", "waiter-enter"]


async def test_a_cancelled_waiter_does_not_release_the_holders_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancelling a *waiter* must not tear down the lock the holder has.

    The other direction of the cancellation contract. The waiter never
    acquired anything, so anything it does on the way out must be nothing --
    an implementation that wrapped the acquire and the release in one
    ``try``/``finally`` would close the holder's fd and unlink its lock file
    out from under a request that is still running, admitting the next
    caller into the critical section.
    """
    from lies.qmd import lock as lock_mod

    monkeypatch.setenv("LIES_QMD_LOCK_PATH", str(tmp_path / "qmd.lock"))

    async def waiter() -> None:
        async with lock_mod.qmd_lock_async(timeout_s=30.0):
            pytest.fail("the waiter acquired a lock this test is holding")

    task = asyncio.create_task(waiter())  # depth-0 snapshot; see above

    async with lock_mod.qmd_lock_async(timeout_s=5.0):
        # Long enough for the waiter to reach its first poll and be parked
        # inside the wait, not still at task start.
        await asyncio.sleep(0.02)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert lock_mod._lock_paths()[0].exists(), (
            "the cancelled waiter released a lock it never held"
        )

    assert not lock_mod._lock_paths()[0].exists()
