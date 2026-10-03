"""Cross-process flock envelope for qmd CLI helpers.

Every :mod:`lies.qmd.cli` helper that shells out to ``qmd`` is wrapped
in :func:`with_qmd_lock`. Concurrent subprocess callers race the
CUDA VMM pool reservation (``cuMemAddressReserve``); the flock makes
that race unreachable by serializing through one site-wide inode.

Lock path: ``${LIES_QMD_LOCK_PATH:-${XDG_STATE_HOME:-~/.local/state}/lies}/qmd.lock``.
Pid + state siblings follow the existing envelope convention in
:mod:`lies.utils.exclusive`.

Wait budget: 30 s poll-retry; past 30 s, :class:`QmdLockBusy` raises.
Heartbeat ``max_age_s``: 1800 s (matches :func:`qmd_embed`'s 30-min wall budget).
"""

from __future__ import annotations

import contextvars
import functools
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable

from lies.lock_errors import (
    QmdLockBusy,
    WikiFlockIndeterminate,
)
from lies.utils.exclusive import (
    acquire_create_lock,
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

    Honors ``LIES_QMD_LOCK_PATH`` (sets the full path), then
    ``XDG_STATE_HOME`` (Linux-style), then ``~/.local/state``.
    """
    state_root = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return Path(state_root) / "lies"


def _lock_paths() -> tuple[Path, Path, Path]:
    """Return (``_LOCK_PATH``, ``_PID_PATH``, ``_STATE_PATH``).

    Module-level constants so test code and the operator CLI can read
    them. Resolved on every call so environment changes between
    acquisitions are honored — the operator CLI process is short-lived
    enough that re-resolution per call costs nothing meaningful.
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


_LOCK_PATH, _PID_PATH, _STATE_PATH = _lock_paths()


def _register_holder(fd: int) -> None:
    """Write the holder pid + heartbeat so contending callers see us as a live holder.

    Best-effort: failures (e.g., transient ``OSError`` on a full disk) are
    logged at WARN and swallowed. The create-lock already serializes
    contenders, so the *caller* is safe; the envelope files are advisory
    metadata that diagnostic tools and ``pid_alive_fn`` read to decide
    whether to reap a stale holder.
    """
    try:
        pid = os.getpid()
        write_owner_pid(_PID_PATH, pid)
        write_heartbeat(
            _STATE_PATH,
            Heartbeat(pid=pid, started_at=time.time(), scope="qmd-cli"),
        )
    except OSError as exc:
        _log.warning("qmd lock holder registration failed: %s", exc)


def _acquire_with_poll(
    retry_budget_s: float,
    max_age_s: float,
) -> int:
    """Poll-retry until the qmd flock is acquired.

    Returns the fd on success. Raises:
    - :class:`QmdLockBusy` after ``retry_budget_s`` elapses while contended
      against a live holder. Carries ``holder_pid``, ``waited_s``, and
      ``max_s`` fields for diagnostics.
    - :class:`WikiFlockIndeterminate` if the envelope reports indeterminate
      — operator must run ``lies flock qmd force-repair``.

    Poll cadence: 100 ms. Deadline check happens before each sleep, so a
    single contended call near the boundary resolves in at most one poll
    interval past ``retry_budget_s``.
    """
    # Resolved here, not read from the module constant. The constant is
    # frozen at import, which silently defeats anything that sets the
    # environment afterwards -- a test's per-session pin, or an operator
    # changing XDG_STATE_HOME between operations. This is what
    # ``_lock_paths`` has always claimed to do ("Resolved on every call so
    # environment changes between acquisitions are honored"); the claim
    # was false until here, and the docstring said so before the code did.
    #
    # Cost is one env read per acquisition. Acquiring is an flock open
    # with a poll loop; the read is not measurable next to it.
    lock_path, pid_path, state_path = _lock_paths()
    deadline = time.monotonic() + retry_budget_s
    started_at = time.monotonic()
    while True:
        result = acquire_create_lock(
            lock_path,
            max_age_s=max_age_s,
            pid_path=pid_path,
            state_json_path=state_path,
        )
        if result is None:
            # Legacy path: only hit if ``exclusive.py`` raises the
            # non-envelope ``None``-on-busy. We always pass the
            # envelope (pid_path + state_json_path), so this branch
            # is defensive.
            if time.monotonic() >= deadline:
                raise QmdLockBusy(
                    waited_s=time.monotonic() - started_at,
                    max_s=retry_budget_s,
                )
            time.sleep(_POLL_INTERVAL_S)
            continue

        if result.status in ("acquired", "dead_reaped"):
            _register_holder(result.fd)
            return result.fd

        if result.status == "indeterminate":
            raise WikiFlockIndeterminate(
                f"qmd lock holder pid {result.holder_pid} indeterminate; "
                f"run `lies flock qmd force-repair`"
            )

        # status == "busy"
        if time.monotonic() >= deadline:
            raise QmdLockBusy(
                holder_pid=result.holder_pid,
                waited_s=time.monotonic() - started_at,
                max_s=retry_budget_s,
            )
        time.sleep(_POLL_INTERVAL_S)


def _release(fd: int) -> None:
    """Best-effort release of the create-lock triad.

    Calls :func:`release_create_lock` with the resolved paths. Safe to
    call when ``fd`` is invalid (raises ``OSError`` is caught and logged).
    """
    # Re-resolved for the same reason as the acquire path: releasing
    # through a stale constant would unlock a file this call never held
    # (and leave the one it did hold locked). The environment is
    # unchanged within a single decorated call, so this normally
    # resolves to the same path the acquire used.
    lock_path, pid_path, state_path = _lock_paths()
    try:
        release_create_lock(lock_path, fd, pid_path=pid_path, state_json_path=state_path)
    except OSError as exc:
        _log.warning("release_create_lock raised: %s", exc)


#: Reentrancy depth for :func:`with_qmd_lock`, keyed on the *context*
#: rather than the thread. A ``ContextVar`` is isolated per thread (a new
#: thread starts from the default) and per asyncio task (a new task copies
#: its parent's context at creation), so neither can observe another's
#: hold. ``threading.local`` isolates threads but is shared by every task
#: on a thread.
_held_depth: contextvars.ContextVar[int] = contextvars.ContextVar("lies_qmd_lock_depth", default=0)


def with_qmd_lock(
    *,
    timeout_s: float = 30.0,
    max_age_s: float = 1800.0,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator factory. Wraps a function so each invocation holds the
    qmd site-wide flock for the duration of the wrapped call.

    Args:
        timeout_s: Wall-clock seconds to poll-retry when contended. Past
            this, ``QmdLockBusy`` is raised. Default 30 s per the spec.
        max_age_s: Wall-clock staleness budget for the envelope — if a
            contending holder's heartbeat is older than this and the
            stored pid is dead/missing, the envelope reaps and retries.
            Default 1800 s (30 min) matches ``qmd_embed``'s wall budget.

    Returns a decorator. The decorator's wrapper acquires on entry,
    releases on exit (including exceptions).

    **Reentrant.** A nested call in the same thread does not re-acquire.
    ``qmd_collection_add_or_update`` calls ``qmd_collection_show`` and
    ``qmd_collection_add``, each of which is itself decorated, so without
    this the outer call holds the flock while the inner one polls for it
    and times out against *itself* with ``QmdLockBusy``. That never fired
    while the acquire path used the import-time constant: the inner
    acquire opened a *different* file (whatever the frozen constant
    happened to be), so it never contended, and the nesting was invisible.
    Per-acquisition resolution exposed it.

    That is the *only* composed case. ``qmd_cleanup`` and
    ``qmd_reindex(cleanup=True)`` both call ``_proc.run(["cleanup"], ...)``
    directly rather than going through each other, so neither nests.
    An earlier version of this docstring named them as a second example;
    it was wrong, and a future maintainer reading it to find a second
    nesting would not find one.

    Reentrancy is keyed on the **context**, not the thread, so a genuine
    second thread still contends and is still serialized -- which is the
    point of the lock. The nesting here is same-context composition, not
    concurrency, and serializing it against itself is a deadlock, not
    safety.

    On *why* context and not thread: every helper this decorates is
    synchronous, and a synchronous function runs to completion without
    yielding, so two of them cannot interleave on one thread -- on today's
    call paths ``threading.local`` and ``ContextVar`` behave identically.
    ``ContextVar`` is the keying that keeps holding if that ever stops
    being true (a decorated helper that awaits, or a lock held across a
    task boundary that runs a *sibling* task rather than a child of the
    holder): it is isolated per thread, since a new thread starts from the
    default, and per task, since a new task copies its parent's context
    but not a sibling's. Note the "copies its parent's" half cuts the
    other way -- a task *spawned by* the holder inherits the depth and is
    correctly treated as already holding, which is the nesting this
    decorator exists for.
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(
            fn
        )  # sets __wrapped__, __name__, __doc__ — the meta-test reads __wrapped__
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
                # reset(), not `set(0)`: it restores the exact prior value
                # and cannot clobber a depth an inner frame raised.
                _held_depth.reset(token)
                _release(fd)

        return wrapper

    return decorator
