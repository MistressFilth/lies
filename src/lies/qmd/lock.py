"""Cross-process flock envelope for the qmd CLI helpers and the daemon query path.

Every :mod:`lies.qmd.cli` helper that shells out to ``qmd`` is
wrapped in :func:`with_qmd_lock`. Concurrent subprocess callers race
the CUDA VMM pool reservation; the flock makes that race unreachable
by serializing through one site-wide inode.

Lock path: ``${LIES_QMD_LOCK_PATH:-${XDG_STATE_HOME:-~/.local/state}/lies}/qmd.lock``.
Each acquirer owns its own wait budget and carries it onto the raised
instance's ``max_s``: the decorator polls for 30 s,
:func:`qmd_lock_async` for 120 s (overridable per call via
``LIES_QMD_QUERY_LOCK_WAIT``, read by :func:`query_lock_wait_s`).
Heartbeat ``max_age_s`` is 1800 s on both.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import logging
import math
import os
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

import anyio

from lies.lock_errors import (
    QmdLockBusy,
    WikiFlockIndeterminate,
)
from lies.utils.exclusive import (
    acquire_create_lock,
    ensure_lock_parent,
    release_create_lock,
)
from lies.utils.lock_heartbeat import (
    Heartbeat,
    write_heartbeat,
    write_owner_pid,
)

_log = logging.getLogger(__name__)

_POLL_INTERVAL_S = 0.1


def _default_lock_dir() -> Path:
    """Resolve the default directory for the qmd site-wide lock.

    Honors ``LIES_QMD_LOCK_PATH``, then ``XDG_STATE_HOME``, then
    ``~/.local/state``.
    """
    state_root = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return Path(state_root) / "lies"


def _lock_paths() -> tuple[Path, Path, Path]:
    """Returns the lock triad for the current environment.

    Resolved on every call so a ``LIES_QMD_LOCK_PATH`` set after import
    is honored.
    """
    explicit = os.environ.get("LIES_QMD_LOCK_PATH")
    if explicit:
        lock_path = Path(explicit).resolve()
    else:
        lock_dir = _default_lock_dir()
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_path = (lock_dir / "qmd.lock").resolve()
    pid_path = Path(f"{lock_path}.pid")
    state_path = Path(f"{lock_path}.state.json")
    return lock_path, pid_path, state_path


def _register_holder(fd: int, paths: tuple[Path, Path, Path] | None = None) -> None:
    """Writes the holder pid + heartbeat. Best-effort: ``OSError`` is logged.

    ``paths`` is the triad the *acquire* resolved. Resolving again here
    would put the two sides of one acquisition on different files if
    ``LIES_QMD_LOCK_PATH`` moved in between.
    """
    _, pid_path, state_path = paths if paths is not None else _lock_paths()
    try:
        pid = os.getpid()
        write_owner_pid(pid_path, pid)
        write_heartbeat(
            state_path,
            Heartbeat(pid=pid, started_at=time.time(), scope="qmd-cli"),
        )
    except OSError as exc:
        _log.warning("qmd lock holder registration failed: %s", exc)


@dataclass(frozen=True)
class _Attempt:
    """One non-blocking shot at the qmd flock.

    Split out of the poll loops because there are two of them, and the
    acquire, the reap and the escalation must not drift between them.
    ``fd`` is ``-1`` on every losing outcome. ``paths`` is the triad the
    attempt ran against, carried out so the release unlinks *these* files.
    """

    outcome: Literal["won", "busy", "absent"]
    fd: int = -1
    holder_pid: int | None = None
    paths: tuple[Path, Path, Path] | None = None


def _attempt_acquire(paths: tuple[Path, Path, Path], max_age_s: float) -> _Attempt:
    """Tries once to take the flock. Never blocks, never sleeps.

    Returns ``"won"``, ``"busy"``, or ``"absent"`` for the envelope-less
    ``None``. Raises :class:`WikiFlockIndeterminate` when the contender's
    liveness cannot be determined.
    """
    lock_path, pid_path, state_path = paths
    result = acquire_create_lock(
        lock_path,
        max_age_s=max_age_s,
        pid_path=pid_path,
        state_json_path=state_path,
        # Depth counter, not pid equality: a second thread of this
        # process stores the same pid, so same-pid self-recovery would
        # reap a lock this process still holds. See AGENTS.md.
        self_acquire_is_stale=False,
        # The caller prepared the parent once for the whole acquisition.
        # See ``_prepare`` below.
        parent_prepared=True,
    )
    if result is None:
        # Defensive: only reached if ``exclusive.py`` returns the
        # non-envelope ``None``-on-busy.
        return _Attempt(outcome="absent")
    if result.status in ("acquired", "dead_reaped"):
        _register_holder(result.fd, paths)
        return _Attempt(outcome="won", fd=result.fd, paths=paths)
    if result.status == "indeterminate":
        raise WikiFlockIndeterminate(
            f"qmd lock holder pid {result.holder_pid} indeterminate; "
            f"run `lies flock qmd force-repair`"
        )
    # status == "busy"
    return _Attempt(outcome="busy", holder_pid=result.holder_pid)


def _prepare(lock_paths: tuple[Path, Path, Path]) -> None:
    """Per-acquisition setup shared by both poll loops.

    Adds the lock *directory* to the triad the caller already resolved, so
    the create does not make one per poll interval. Not a lock on the
    directory's lifetime: the create still heals a vanished parent.
    """
    ensure_lock_parent(lock_paths[0])


def _acquire_with_poll(
    retry_budget_s: float,
    max_age_s: float,
) -> _Attempt:
    """Poll-retry until the qmd flock is acquired.

    Blocking: every wait is a :func:`time.sleep`, so this is for sync call
    sites only. An async caller should use :func:`qmd_lock_async`.

    Returns:
        The winning :class:`_Attempt`, whose ``fd`` *and* ``paths`` the
        caller hands straight back to :func:`_release`.

    Raises:
        QmdLockBusy: The budget elapsed with a live contender holding the
            lock.
        WikiFlockIndeterminate: The contender's liveness is undeterminable.
    """
    lock_paths = _lock_paths()
    _prepare(lock_paths)
    deadline = time.monotonic() + retry_budget_s
    started_at = time.monotonic()
    while True:
        attempt = _attempt_acquire(lock_paths, max_age_s)
        if attempt.outcome == "won":
            return attempt
        if time.monotonic() >= deadline:
            raise QmdLockBusy(
                holder_pid=attempt.holder_pid,
                waited_s=time.monotonic() - started_at,
                max_s=retry_budget_s,
                lock_path=lock_paths[0],
            )
        time.sleep(_POLL_INTERVAL_S)


async def _acquire_with_poll_async(
    retry_budget_s: float,
    max_age_s: float,
) -> _Attempt:
    """:func:`_acquire_with_poll` with an ``await`` between attempts.

    Same acquire, reaping and raise semantics. The sleep is clamped to
    what is left of the budget, so a short budget overshoots by at most
    one extra attempt.
    """
    lock_paths = _lock_paths()
    _prepare(lock_paths)
    deadline = time.monotonic() + retry_budget_s
    started_at = time.monotonic()
    while True:
        attempt = _attempt_acquire(lock_paths, max_age_s)
        if attempt.outcome == "won":
            return attempt
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise QmdLockBusy(
                holder_pid=attempt.holder_pid,
                waited_s=time.monotonic() - started_at,
                max_s=retry_budget_s,
                lock_path=lock_paths[0],
            )
        await anyio.sleep(min(_POLL_INTERVAL_S, remaining))


def _release(fd: int, paths: tuple[Path, Path, Path] | None = None) -> None:
    """Best-effort release of the create-lock triad.

    ``paths`` is the triad the acquire resolved, so the release unlinks
    the files *this* acquisition created. Re-resolving splits one
    acquisition across two sets of files when ``LIES_QMD_LOCK_PATH``
    moves in between, leaving a stale heartbeat the next acquire reads.
    """
    lock_path, pid_path, state_path = paths if paths is not None else _lock_paths()
    try:
        release_create_lock(lock_path, fd, pid_path=pid_path, state_json_path=state_path)
    except OSError as exc:
        _log.warning("release_create_lock raised: %s", exc)


#: Reentrancy depth, keyed on the *context* (``ContextVar``), so a
#: second thread still contends — ``threading.local`` would share
#: across tasks on the same thread and disable the lock.
_held_depth: contextvars.ContextVar[int] = contextvars.ContextVar("lies_qmd_lock_depth", default=0)


def with_qmd_lock(
    *,
    timeout_s: float = 30.0,
    max_age_s: float = 1800.0,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator factory; each invocation holds the qmd site-wide flock for
    the duration of the wrapped call.

    Args:
        timeout_s: Wall-clock seconds to poll-retry when contended;
            ``QmdLockBusy`` past this. Default 30 s.
        max_age_s: Staleness budget — a contending holder whose
            heartbeat is older and whose pid is dead is reaped and
            retried. Default 1800 s matches ``qmd_embed``.

    Reentrant on the same context, because some helpers compose: an
    outer call would otherwise poll an inner one to a timeout.
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)  # sets __wrapped__ — the meta-test reads it
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            state = _held_depth.get()
            if state:
                # Already held in this context: run without re-acquiring.
                token = _held_depth.set(state + 1)
                try:
                    return fn(*args, **kwargs)
                finally:
                    _held_depth.reset(token)

            attempt = _acquire_with_poll(retry_budget_s=timeout_s, max_age_s=max_age_s)
            token = _held_depth.set(1)
            try:
                return fn(*args, **kwargs)
            finally:
                # reset(), not ``set(0)``: it restores the exact prior
                # value and cannot clobber a depth an inner frame raised.
                _held_depth.reset(token)
                _release(attempt.fd, attempt.paths)

        return wrapper

    return decorator


@contextlib.asynccontextmanager
async def qmd_lock_async(
    *,
    timeout_s: float = 120.0,
    max_age_s: float = 1800.0,
) -> AsyncIterator[None]:
    """Async twin of :func:`with_qmd_lock`, for the daemon query path.

    Same flock, heartbeat, reaping and :class:`QmdLockBusy`; the wait
    *awaits* instead of calling :func:`time.sleep`, so it parks no
    anyio thread token. Reentrant on the same context; two *tasks*
    contend. See AGENTS.md for the reentrancy hazard that shape
    carries.

    Args:
        timeout_s: Wall-clock seconds to poll-retry when contended;
            ``QmdLockBusy`` past this. Default 120 s.
        max_age_s: Staleness budget for a dead contender's heartbeat.
            Default 1800 s, matching :func:`with_qmd_lock`.

    Yields:
        None. The fd is released in a ``finally``, so an exception and a
        cancellation both unwind through it.
    """
    state = _held_depth.get()
    if state:
        # Already held in this context: run without re-acquiring.
        token = _held_depth.set(state + 1)
        try:
            yield None
        finally:
            _held_depth.reset(token)
        return

    attempt = await _acquire_with_poll_async(retry_budget_s=timeout_s, max_age_s=max_age_s)
    token = _held_depth.set(1)
    try:
        yield None
    finally:
        # reset(), not ``set(0)``: it restores the exact prior value and
        # cannot clobber a depth an inner frame raised.
        _held_depth.reset(token)
        _release(attempt.fd, attempt.paths)


#: Wait budget for the daemon query path, in seconds.
_QUERY_LOCK_WAIT_S = 120.0
_QUERY_LOCK_WAIT_ENV = "LIES_QMD_QUERY_LOCK_WAIT"


def query_lock_wait_s() -> float:
    """Wait budget in seconds for the qmd query lock, read per call.

    Anything that is not a positive, finite number — ``inf`` included —
    falls back to the default rather than raising: this is read on the
    retrieval path, and a typo in a shell profile must not fail every
    query.
    """
    raw = os.environ.get(_QUERY_LOCK_WAIT_ENV)
    if raw is None:
        return _QUERY_LOCK_WAIT_S
    try:
        value = float(raw)
    except ValueError:
        return _QUERY_LOCK_WAIT_S
    return value if (math.isfinite(value) and value > 0) else _QUERY_LOCK_WAIT_S
