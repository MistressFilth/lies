"""Cross-process flock envelope for qmd CLI helpers.

Every :mod:`lies.qmd.cli` helper that shells out to ``qmd`` is
wrapped in :func:`with_qmd_lock`. Concurrent subprocess callers
race the CUDA VMM pool reservation (``cuMemAddressReserve``);
the flock makes that race unreachable by serializing through
one site-wide inode.

Lock path: ``${LIES_QMD_LOCK_PATH:-${XDG_STATE_HOME:-~/.local/state}/lies}/qmd.lock``.
Wait budget: 30 s poll-retry; past 30 s, :class:`QmdLockBusy`
raises. Heartbeat ``max_age_s``: 1800 s (matches
:func:`qmd_embed`'s 30-min wall budget).
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


def _acquire_with_poll(
    retry_budget_s: float,
    max_age_s: float,
) -> int:
    """Poll-retry until the qmd flock is acquired. Raises:
    - :class:`QmdLockBusy` past ``retry_budget_s``.
    - :class:`WikiFlockIndeterminate` if the envelope is indeterminate.
    """
    lock_path, pid_path, state_path = _lock_paths()
    deadline = time.monotonic() + retry_budget_s
    started_at = time.monotonic()
    while True:
        result = acquire_create_lock(
            lock_path,
            max_age_s=max_age_s,
            pid_path=pid_path,
            state_json_path=state_path,
            # Re-entry is tracked by the depth counter above, not by
            # pid equality. A second *thread* of this process stores
            # the same pid, so the default same-pid self-recovery
            # would reap a lock this thread is still holding and let
            # both into the critical section — which is the CUDA
            # reservation race this lock exists to prevent.
            self_acquire_is_stale=False,
        )
        if result is None:
            # Defensive: only reached if ``exclusive.py`` raises
            # the non-envelope ``None``-on-busy.
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
