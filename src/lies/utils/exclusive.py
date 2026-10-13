"""Cross-process exclusive-create lock."""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from lies.utils.lock_heartbeat import (
    AcquireResult,
    read_heartbeat,
    read_owner_pid,
)

_log = logging.getLogger(__name__)

MAX_FLOCK_AGE_S = 2 * 3600  # 2h ceiling on memory flock liveness


class _IndeterminateMarker(Exception):
    """Internal sentinel: pid_alive_fn returned 'indeterminate' on a stale heartbeat."""

    def __init__(self, holder_pid: int, holder_started_at: float) -> None:
        super().__init__(holder_pid, holder_started_at)
        self.holder_pid = holder_pid
        self.holder_started_at = holder_started_at


def ensure_lock_parent(path: Path) -> None:
    """Creates ``path.parent`` if it does not exist.

    Split out for polling callers, which re-run the whole acquire once
    per poll interval and must not pay the ``mkdir`` each time.
    """
    path.parent.mkdir(parents=True, exist_ok=True)


def _create_exclusive(path: Path) -> int:
    """``O_CREAT | O_EXCL`` create, healing a missing parent first.

    ``FileNotFoundError`` never escapes; ``FileExistsError`` is the
    contention signal every caller already handles.
    """
    try:
        return os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileNotFoundError:
        ensure_lock_parent(path)
        return os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)


def acquire_create_lock(  # type: ignore[no-untyped-def]
    path: Path,
    *,
    max_age_s: float,
    pid_path: Path | None = None,
    state_json_path: Path | None = None,
    pid_alive_fn: Callable[[int], Literal["alive", "dead", "indeterminate"]] | None = None,
    force_repair: bool = False,
    self_acquire_is_stale: bool = True,
    parent_prepared: bool = False,
) -> AcquireResult | None:
    """Atomically create ``path``. Return ``AcquireResult`` on win, ``None`` on contention.

    With ``pid_path`` + ``state_json_path``, an existing create-lock whose stored PID is dead (or whose heartbeat
    is older than ``max_age_s``) is treated as a stale holder, reaped once, and the create retried.
    ``force_repair=True`` escalates that second chance to a seemingly live contender; without it a live holder
    always wins. See the module docstring for the exception policy, and AGENTS.md "Data shapes" for the four
    ``AcquireResult.status`` outcomes.

    ``self_acquire_is_stale=False`` disables same-pid self-recovery, which a caller tracking re-entry itself must
    set: the default assumes a same-pid lock is this process's own, which a second *thread* falsifies.
    ``parent_prepared=True`` is only an optimisation. The fd stays open so the OS holds the inode.
    """
    if pid_alive_fn is None:

        def pid_alive_fn(
            pid: int,
        ) -> Literal["alive", "dead", "indeterminate"]:
            try:
                os.kill(pid, 0)
                return "alive"
            except ProcessLookupError:
                return "dead"  # ESRCH — dead, reap
            except PermissionError:
                return "indeterminate"  # EPERM — different user/perm; cannot determine
            except OSError:
                _log.warning("pid_alive(%s) raised non-ESRCH/EPERM OSError", pid)
                return "indeterminate"  # unknown syscall failure; treat conservatively

    def _wrap(fd: int, status: Literal["acquired", "dead_reaped"] = "acquired") -> AcquireResult:
        return AcquireResult(fd=fd, status=status)

    if not parent_prepared:
        ensure_lock_parent(path)
    try:
        return _wrap(_create_exclusive(path))
    except FileExistsError:
        if pid_path is not None and state_json_path is not None:
            if force_repair:
                # Unconditional reap + retry once. The caller will
                # raise WikiFlockUnrepairable if the retry still loses.
                _reap_files(path, pid_path, state_json_path)
                try:
                    return _wrap(_create_exclusive(path), status="dead_reaped")
                except FileExistsError:
                    return None
            try:
                if _reap_if_stale(
                    path,
                    pid_path,
                    state_json_path,
                    max_age_s,
                    pid_alive_fn,
                    self_acquire_is_stale=self_acquire_is_stale,
                ):
                    # Reap removed the create-lock; retry the create once.
                    try:
                        return _wrap(_create_exclusive(path), status="dead_reaped")
                    except FileExistsError:
                        return None
            except _IndeterminateMarker as marker:
                return AcquireResult(
                    fd=-1,
                    holder_pid=marker.holder_pid,
                    holder_started_at=marker.holder_started_at,
                    status="indeterminate",
                )
            # Live contender; populate holder info from the files we
            # just inspected so raise sites can name the pid.
            holder_pid = read_owner_pid(pid_path)
            heartbeat = read_heartbeat(state_json_path)
            holder_started_at = heartbeat.started_at if heartbeat is not None else None
            return AcquireResult(
                fd=-1,
                holder_pid=holder_pid,
                holder_started_at=holder_started_at,
                status="busy",
            )
        # Legacy orphan-only path: no pid/state supplied -> use mtime window.
        if not _is_orphaned(path, max_age_s):
            return None
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        try:
            return _wrap(_create_exclusive(path))
        except FileExistsError:
            return None


def _reap_if_stale(  # type: ignore[no-untyped-def]
    create_lock: Path,
    pid_path: Path,
    state_json_path: Path,
    max_age_s: float,
    pid_alive_fn: Callable[[int], Literal["alive", "dead", "indeterminate"]],
    *,
    self_acquire_is_stale: bool = True,
) -> bool:
    """Return True if reap happened (caller should retry), False otherwise."""
    if not pid_path.exists():
        return False
    try:
        stored_pid = int(pid_path.read_text(encoding="utf-8").strip())
    except OSError, ValueError:
        _log.warning("could not parse pid file %s", pid_path)
        return False

    if stored_pid == os.getpid() and self_acquire_is_stale:
        # Self-acquire: not contended. Reap the stale lock and let retry succeed.
        _reap_files(create_lock, pid_path, state_json_path)
        return True

    classification = pid_alive_fn(stored_pid)
    if classification == "dead":
        # Reap + return True so caller retries the create.
        _reap_files(create_lock, pid_path, state_json_path)
        return True
    if classification == "alive":
        # Spec: never reap a live process. Surface as busy regardless of wall-clock.
        return False
    # classification == "indeterminate"
    started_at: float = 0.0
    if state_json_path.exists():
        try:
            payload = json.loads(state_json_path.read_text(encoding="utf-8"))
            started_at = float(payload.get("started_at", 0.0))
        except OSError, ValueError, json.JSONDecodeError:
            started_at = 0.0
    if _heartbeat_fresh(started_at, max_age_s):
        return False  # fresh heartbeat; treat as busy
    # Stale heartbeat + indeterminate → caller routes to WikiFlockIndeterminate.
    raise _IndeterminateMarker(stored_pid, started_at)


def _heartbeat_fresh(started_at: float, max_age_s: float) -> bool:
    """Return True when ``started_at`` is within ``max_age_s`` of now."""
    return (time.time() - started_at) < max_age_s


def _reap_files(create_lock: Path, pid_path: Path, state_json_path: Path) -> None:
    """Best-effort unlink of the three lock sibling files. Order matters
    only for operator debugging; no atomicity required."""
    for p in (state_json_path, pid_path, create_lock):
        try:
            p.unlink()
        except FileNotFoundError:
            pass


def _is_orphaned(path: Path, max_age_s: float) -> bool:
    """Return True if ``path`` exists and is older than the recovery window."""
    try:
        stat = path.stat()
    except FileNotFoundError:
        return False
    return (time.time() - stat.st_mtime) > max_age_s


def release_create_lock(  # type: ignore[no-untyped-def]
    path: Path,
    fd: int | None,
    *,
    pid_path: Path | None = None,
    state_json_path: Path | None = None,
) -> None:
    """Close ``fd`` (if any) and unlink lock + pid + state. Safe to call twice."""
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass
    for p in (state_json_path, pid_path, path):
        if p is None:
            continue
        try:
            p.unlink()
        except FileNotFoundError:
            pass
