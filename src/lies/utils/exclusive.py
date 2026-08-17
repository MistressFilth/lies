"""Cross-process exclusive-create lock.

``acquire_create_lock`` closes the read-then-write race on any state
file: the caller reads state, decides, then writes — two processes can
both observe "free" and both win. Holding an ``O_CREAT | O_EXCL`` create
on a sibling path across that whole sequence makes the decision
single-winner, because the OS guarantees exactly one create succeeds.

Stale recovery: the caller may optionally pre-write a holder PID
file (``write_owner_pid`` from :mod:`lies.utils.lock_heartbeat`) at the
``pid_path`` argument and a heartbeat JSON at ``state_json_path``. When
those files exist and a competing caller observes the create-lock as
held, it reads the PID and asks ``pid_alive_fn`` (default:
``os.kill(pid, 0)``) whether the holder is alive. A dead holder yields
reap + one retry; a live holder yields ``None`` (busy). Same-PID
recovery treats ``stored == os.getpid()`` as recovery. Wall-clock
recovery treats heartbeats older than ``max_age_s`` as stale. Exception
policy: ``ProcessLookupError`` (ESRCH) -> reap; ``PermissionError``
(EPERM) -> busy, never reap; unexpected ``OSError`` -> busy + WARN log.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

_log = logging.getLogger(__name__)

MAX_FLOCK_AGE_S_DEFAULT = 2 * 3600


def acquire_create_lock(  # type: ignore[no-untyped-def]
    path: Path,
    *,
    max_age_s: float,
    pid_path: Path | None = None,
    state_json_path: Path | None = None,
    pid_alive_fn=None,
) -> int | None:
    """Atomically create ``path``. Return the open fd on win, ``None`` on contention.

    Optional ``pid_path`` + ``state_json_path``: if provided, treat an
    existing create-lock as a stale holder when its stored PID is
    dead (or its heartbeat is older than ``max_age_s``) and reap + retry
    once. See the module docstring for the exception policy.

    The fd is left open so the OS holds the inode reference; the caller
    must pass it to :func:`release_create_lock`.
    """
    if pid_alive_fn is None:

        def pid_alive_fn(pid: int) -> bool:
            try:
                os.kill(pid, 0)
                return True
            except ProcessLookupError:
                return False  # ESRCH — dead, reap
            except PermissionError:
                return True  # EPERM — different user/perm; treat as busy
            except OSError:
                _log.warning("pid_alive(%s) raised non-ESRCH/EPERM OSError", pid)
                return True  # unknown syscall failure; treat as busy

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        return os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        if pid_path is not None and state_json_path is not None:
            if not _reap_if_stale(path, pid_path, state_json_path, max_age_s, pid_alive_fn):
                return None
            # Reap removed the create-lock; retry the create once.
            try:
                return os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                return None
        # Legacy orphan-only path: no pid/state supplied -> use mtime window.
        if not _is_orphaned(path, max_age_s):
            return None
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        try:
            return os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return None


def _reap_if_stale(  # type: ignore[no-untyped-def]
    create_lock: Path,
    pid_path: Path,
    state_json_path: Path,
    max_age_s: float,
    pid_alive_fn,
) -> bool:
    """Return True if reap happened (caller should retry), False otherwise.

    Reap triggers:
      - pid file missing: race; treat as non-stale (caller treats as busy).
      - stored PID == os.getpid(): self-recovery; reap + retry.
      - pid_alive_fn(stored) is False: dead; reap + retry.
      - heartbeat started_at is older than (now - max_age_s): wall-clock stale; reap + retry.

    Reap links the create-lock, the pid file, and the state JSON. The
    caller is responsible for the follow-up retry on True.
    """
    if not pid_path.exists():
        return False
    # fmt: off
    try:
        stored_pid = int(pid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        _log.warning("could not parse pid file %s", pid_path)
        return False
    # fmt: on

    if stored_pid == os.getpid():
        # Self-acquire: not contended. Reap the stale lock and let retry succeed.
        _reap_files(create_lock, pid_path, state_json_path)
        return True

    # pid_alive_fn may raise on EPERM; classifier already handles that.
    if pid_alive_fn(stored_pid):
        # Holder alive. Check wall-clock window.
        if state_json_path.exists():
            # fmt: off
            try:
                payload = json.loads(state_json_path.read_text(encoding="utf-8"))
                started_at = float(payload.get("started_at", 0.0))
            except (OSError, ValueError, json.JSONDecodeError):
                started_at = 0.0
            # fmt: on
            if (time.time() - started_at) < max_age_s:
                return False  # alive and fresh: busy.
        else:
            # Heartbeat missing but holder alive: treat as busy.
            return False

    _reap_files(create_lock, pid_path, state_json_path)
    return True


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
