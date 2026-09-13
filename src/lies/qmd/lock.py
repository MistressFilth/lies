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

import functools
import logging
import os
from pathlib import Path
from typing import Any, Callable

from lies.lock_errors import (  # noqa: F401 — Task 3 will use these from this module.
    QmdLockBusy,
    WikiFlockIndeterminate,
)
from lies.utils.exclusive import (
    acquire_create_lock,  # noqa: F401 — Task 3 will use this.
    release_create_lock,
)
from lies.utils.lock_heartbeat import (
    Heartbeat,  # noqa: F401 — Task 3 will use this.
    write_heartbeat,  # noqa: F401 — Task 3 will use this.
    write_owner_pid,  # noqa: F401 — Task 3 will use this.
)

_log = logging.getLogger(__name__)


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


def _acquire_with_poll(
    retry_budget_s: float,
    max_age_s: float,
) -> int:
    """Poll-retry until ``acquire_create_lock`` returns ``acquired``.

    Returns the fd on success. Raises:
    - :class:`QmdLockBusy` after ``retry_budget_s`` elapses while contended.
    - :class:`WikiFlockIndeterminate` if the envelope reports indeterminate.

    Implementation note: real ``_acquire_with_poll`` lives in Task 3.
    This placeholder is the minimum scaffolding the path-resolution
    tests need.
    """
    raise NotImplementedError("filled in by Task 3")


def _release(fd: int) -> None:
    """Best-effort release of the create-lock triad.

    Calls :func:`release_create_lock` with the resolved paths. Safe to
    call when ``fd`` is invalid (raises ``OSError`` is caught and logged).
    """
    try:
        release_create_lock(_LOCK_PATH, fd, pid_path=_PID_PATH, state_json_path=_STATE_PATH)
    except OSError as exc:
        _log.warning("release_create_lock raised: %s", exc)


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
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(
            fn
        )  # sets __wrapped__, __name__, __doc__ — the meta-test reads __wrapped__
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            fd = _acquire_with_poll(retry_budget_s=timeout_s, max_age_s=max_age_s)
            try:
                return fn(*args, **kwargs)
            finally:
                _release(fd)

        return wrapper

    return decorator
