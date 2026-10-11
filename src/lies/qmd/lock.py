"""Cross-process flock envelope for the qmd CLI helpers and the daemon query path.

Every :mod:`lies.qmd.cli` helper that shells out to ``qmd`` is
wrapped in :func:`with_qmd_lock`. Concurrent subprocess callers
race the CUDA VMM pool reservation (``cuMemAddressReserve``);
the flock makes that race unreachable by serializing through
one site-wide inode.

Lock path: ``${LIES_QMD_LOCK_PATH:-${XDG_STATE_HOME:-~/.local/state}/lies}/qmd.lock``.
Wait budget: belongs to the acquirer, so there is no module-wide
number. ``with_qmd_lock`` polls for 30 s and :class:`QmdLockBusy`
raises past it; ``qmd_lock_async`` polls for 120 s, overridable per
call with ``LIES_QMD_QUERY_LOCK_WAIT`` (:func:`query_lock_wait_s`).
Both carry the budget onto the raised instance's ``max_s``, which is
what a caller should read. Heartbeat ``max_age_s``: 1800 s on both
(matches :func:`qmd_embed`'s 30-min wall budget).

The daemon query path is async, so it cannot use the decorator: the poll
loop in :func:`with_qmd_lock` sleeps with ``time.sleep``. What a blocking
poll would cost there is **not** the event loop — see
:func:`qmd_lock_async` for the real mechanism, which is a worker-thread
token. :func:`qmd_lock_async` is the same envelope with an ``await``
between polls, and it is still the right call: the wait is bounded and
yields, so a contended acquire parks neither an event loop nor a scarce
thread token for the length of the wait.
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
    """Return the lock triad for the current environment.

    Resolved on every call so environment changes between
    acquisitions are honored. The production acquire/release
    pair threads its own paths; the heartbeat writer in
    :func:`_register_holder` also calls this helper. A
    module-level constant would have frozen the lock triad at
    import and silently defeated any test that sets
    ``LIES_QMD_LOCK_PATH`` afterwards.
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


def _register_holder(fd: int) -> None:
    """Write the holder pid + heartbeat. Best-effort: ``OSError`` is logged."""
    _, pid_path, state_path = _lock_paths()
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

    Split out of the poll loops because there are now two of them -- one
    that sleeps with ``time.sleep`` and one that awaits -- and the acquire,
    the reap and the indeterminate escalation must not be allowed to drift
    apart between them. A reap-policy change belongs here, once.

    ``fd`` is ``-1`` on every losing outcome, matching
    :class:`~lies.utils.lock_heartbeat.AcquireResult`'s convention.
    """

    outcome: Literal["won", "busy", "absent"]
    fd: int = -1
    holder_pid: int | None = None


def _attempt_acquire(paths: tuple[Path, Path, Path], max_age_s: float) -> _Attempt:
    """Try once to take the flock. Never blocks, never sleeps.

    ``paths`` is the triad resolved by the caller, not resolved here. A
    contended acquire runs one attempt per poll interval for the length of
    its budget, so resolving per attempt would redo a ``Path.resolve()``
    realpath walk at the poll rate for the whole wait. The lock
    directory is hoisted out for the same reason — see :func:`_prepare`.

    Returns ``outcome="won"`` on success -- the holder pid and heartbeat are
    already registered by then -- ``"busy"`` when a live contender holds it,
    or ``"absent"`` for the envelope-less ``None`` that
    :func:`~lies.utils.exclusive.acquire_create_lock` returns.

    Raises:
        :class:`WikiFlockIndeterminate`: the contender's liveness cannot be
            determined. The primitive did not reap.
    """
    lock_path, pid_path, state_path = paths
    result = acquire_create_lock(
        lock_path,
        max_age_s=max_age_s,
        pid_path=pid_path,
        state_json_path=state_path,
        # Re-entry is tracked by the depth counter, not by pid equality.
        # A second *thread* or *task* of this process stores the same
        # pid, so the default same-pid self-recovery would reap a lock
        # this process is still holding and let both into the critical
        # section — which is the CUDA reservation race this lock exists
        # to prevent.
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
        _register_holder(result.fd)
        return _Attempt(outcome="won", fd=result.fd)
    if result.status == "indeterminate":
        raise WikiFlockIndeterminate(
            f"qmd lock holder pid {result.holder_pid} indeterminate; "
            f"run `lies flock qmd force-repair`"
        )
    # status == "busy"
    return _Attempt(outcome="busy", holder_pid=result.holder_pid)


def _prepare(lock_paths: tuple[Path, Path, Path]) -> None:
    """Per-acquisition setup shared by both poll loops.

    Resolving the triad is the caller's job (see :func:`_lock_paths`) and
    is done once, before the loop, as it always was. This adds the lock
    *directory* to that: ``acquire_create_lock`` created its parent on
    every attempt, and an attempt runs once per poll interval for the
    length of the budget. At 10 Hz for 120 s that is ~1200 ``mkdir``
    calls re-asserting a directory that already existed (measured
    2026-10-10: 1 ``mkdir`` + 1 ``stat``, 4.3 us each — ~2400 syscalls
    and ~5.2 ms across a fully contended wait).

    ``_attempt_acquire`` passes ``parent_prepared=True``, so this is the
    only ``mkdir`` an acquisition performs. It is not a lock on the
    directory's lifetime: the create still heals a parent that vanishes
    mid-wait, so a contended poll cannot fail on a directory someone
    removed underneath it.
    """
    ensure_lock_parent(lock_paths[0])


def _acquire_with_poll(
    retry_budget_s: float,
    max_age_s: float,
) -> int:
    """Poll-retry until the qmd flock is acquired. Raises:
    - :class:`QmdLockBusy` past ``retry_budget_s``.
    - :class:`WikiFlockIndeterminate` if the envelope is indeterminate.

    Blocking: every wait is a :func:`time.sleep`, so this is for sync call
    sites only. An async caller that lands here stops the event loop for the
    length of the wait; use :func:`qmd_lock_async` instead.

    The triad is resolved once, before the loop, as it always was — one
    resolution per acquisition, however many attempts that acquisition
    takes.
    """
    lock_paths = _lock_paths()
    _prepare(lock_paths)
    deadline = time.monotonic() + retry_budget_s
    started_at = time.monotonic()
    while True:
        attempt = _attempt_acquire(lock_paths, max_age_s)
        if attempt.outcome == "won":
            return attempt.fd
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
) -> int:
    """:func:`_acquire_with_poll` with an ``await`` between attempts.

    Same acquire, reaping and raise semantics; the only difference is that
    the wait yields to the event loop instead of parking the thread.

    The sleep is clamped to what is left of the budget. A flat
    ``_POLL_INTERVAL_S`` overshoots a short budget by up to a full poll
    interval -- a 50 ms wait costs 100 ms -- and the clamp bounds that to
    one extra attempt.

    Resolved once per acquisition here too. The async path has no legacy
    behaviour to preserve, but it has the same poll rate.
    """
    lock_paths = _lock_paths()
    _prepare(lock_paths)
    deadline = time.monotonic() + retry_budget_s
    started_at = time.monotonic()
    while True:
        attempt = _attempt_acquire(lock_paths, max_age_s)
        if attempt.outcome == "won":
            return attempt.fd
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise QmdLockBusy(
                holder_pid=attempt.holder_pid,
                waited_s=time.monotonic() - started_at,
                max_s=retry_budget_s,
                lock_path=lock_paths[0],
            )
        await anyio.sleep(min(_POLL_INTERVAL_S, remaining))


def _release(fd: int) -> None:
    """Best-effort release of the create-lock triad."""
    lock_path, pid_path, state_path = _lock_paths()
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
    """Decorator factory. Each invocation holds the qmd site-wide
    flock for the duration of the wrapped call.

    Args:
        timeout_s: Wall-clock seconds to poll-retry when contended;
            ``QmdLockBusy`` past this. Default 30 s.
        max_age_s: Staleness budget — if a contending holder's
            heartbeat is older and its pid is dead, the envelope
            reaps and retries. Default 1800 s matches ``qmd_embed``.

    Reentrant on the same context (``: a nested call in the same
    task does not re-acquire. Two composed helpers — e.g.
    ``qmd_collection_add_or_update`` calls
    ``qmd_collection_show`` then ``qmd_collection_add`` — were
    the failure that drove this. Without reentrancy the outer call
    holds the flock while the inner one polls for it and times out
    against itself; with the import-time lock-path constant the
    inner acquire opened a different file (whatever the constant
    pointed at) so the nesting was invisible. Per-acquisition path
    resolution exposed it.
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

            fd = _acquire_with_poll(retry_budget_s=timeout_s, max_age_s=max_age_s)
            token = _held_depth.set(1)
            try:
                return fn(*args, **kwargs)
            finally:
                # reset(), not ``set(0)``: it restores the exact prior
                # value and cannot clobber a depth an inner frame raised.
                _held_depth.reset(token)
                _release(fd)

        return wrapper

    return decorator


@contextlib.asynccontextmanager
async def qmd_lock_async(
    *,
    timeout_s: float = 120.0,
    max_age_s: float = 1800.0,
) -> AsyncIterator[None]:
    """Async twin of :func:`with_qmd_lock`, for the daemon query path.

    Same flock, same heartbeat, same reaping, same
    :class:`QmdLockBusy`; the wait *awaits* instead of calling
    :func:`time.sleep`.

    **What a blocking poll would actually cost here.** An earlier
    version of this docstring said the MCP daemon "runs many requests
    on one event loop, so a contended acquire would freeze every
    concurrent request". That is wrong for the production path, and it
    was asserted without reading the dispatch code. ``search`` is a
    *sync* ``FunctionTool`` (``search = Tool.from_function(_search_impl)``)
    and FastMCP's default is ``run_in_thread=True``, so a sync tool body
    is dispatched through
    ``call_sync_fn_in_threadpool`` → ``anyio.to_thread.run_sync``
    (``fastmcp/tools/function_tool.py:477-481``,
    ``fastmcp/utilities/async_utils.py:26-34``). ``_run_blocking`` then
    finds no running loop on that worker thread and takes
    ``asyncio.run`` — so the wait runs on **its own event loop, on a
    worker thread**. The daemon's serving loop is never blocked.

    The design still stands, for a different and weaker reason: the
    default thread limiter carries 40 tokens (measured with
    ``anyio.to_thread.current_default_thread_limiter().total_tokens``),
    so a blocking poll would park one of 40 for the length of the wait,
    and 40 contended queries would exhaust the pool. Awaiting parks
    none. That is the actual argument for this primitive — not loop
    protection — and the 40 is anyio's default, not a LIES setting.

    Args:
        timeout_s: Wall-clock seconds to poll-retry when contended;
            ``QmdLockBusy`` past this. Default 120 s, well above the CLI
            decorator's 30 s: a query is queued against a daemon that is
            already serving other work rather than starting a subprocess,
            so the budget is spent waiting, not booting.
        max_age_s: Staleness budget for a dead contender's heartbeat.
            Default 1800 s, matching :func:`with_qmd_lock`.

    Reentrant on the same context, exactly as the decorator is: a nested
    acquire in one task runs without re-acquiring. Two *tasks* contend,
    because each snapshots ``_held_depth`` at creation — which is the
    production shape, since two concurrent requests are two tasks.

    **Latent reentrancy hazard — do not build this shape.** ``_held_depth``
    is a :class:`contextvars.ContextVar`, and contextvars propagate
    *into* ``anyio.to_thread.run_sync`` workers (FastMCP says so itself:
    "properly propagates contextvars", ``fastmcp/utilities/async_utils.py``).
    So a future path that acquires the qmd lock and then dispatches a
    ``query`` through ``_run_blocking`` would carry a non-zero
    ``_held_depth`` into the worker thread, take the **reentrant** branch
    above, and run the query with **no inode held** — silently, with no
    error and no log line. Not reachable today: the only
    ``qmd_lock_async`` holder is ``daemon_tool``, and no
    ``with_qmd_lock``-wrapped helper calls ``daemon_tool``. Re-verify
    that before assuming it still holds.

    The fd is released in a ``finally``, so an exception and a cancellation
    both unwind through it (``CancelledError`` is a ``BaseException`` and a
    ``finally`` is the only construct that reliably covers it). Nothing
    between the winning attempt and the ``yield`` awaits, so there is no
    window in which a cancellation could strand an acquired fd.
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

    fd = await _acquire_with_poll_async(retry_budget_s=timeout_s, max_age_s=max_age_s)
    token = _held_depth.set(1)
    try:
        yield None
    finally:
        # reset(), not ``set(0)``: it restores the exact prior value and
        # cannot clobber a depth an inner frame raised.
        _held_depth.reset(token)
        _release(fd)


#: Wait budget for the daemon query path, in seconds. 120 s is the number
#: proven in the sibling ``ask`` project, whose single-flight gates every
#: query reaching the same qmd daemon.
_QUERY_LOCK_WAIT_S = 120.0
_QUERY_LOCK_WAIT_ENV = "LIES_QMD_QUERY_LOCK_WAIT"


def query_lock_wait_s() -> float:
    """Wait budget in seconds for the qmd query lock.

    Read per call so a host can retune it without restarting the daemon.

    Anything that is not a positive, finite number falls back to the default
    rather than raising. This is read on the retrieval path, and raising
    here would turn a typo in a shell profile into every query failing — a
    far worse outcome than ignoring the knob.

    ``inf`` is in that set deliberately: it parses, it passes ``> 0``, and
    it turns the poll into an unbounded wait with no diagnostic anywhere.
    A budget nobody set is exactly as real a mistake as a budget that is
    not a number.
    """
    raw = os.environ.get(_QUERY_LOCK_WAIT_ENV)
    if raw is None:
        return _QUERY_LOCK_WAIT_S
    try:
        value = float(raw)
    except ValueError:
        return _QUERY_LOCK_WAIT_S
    return value if (math.isfinite(value) and value > 0) else _QUERY_LOCK_WAIT_S
