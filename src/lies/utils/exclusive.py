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

Return shape: :func:`acquire_create_lock` returns an
:class:`~lies.utils.lock_heartbeat.AcquireResult` on every successful
acquire (with ``status`` set to ``"acquired"`` or ``"dead_reaped"``)
or on contention with ``pid_path`` / ``state_json_path`` supplied
(``status="busy"``, ``fd=-1``, ``holder_pid`` + ``holder_started_at``
populated). Legacy callers that omit the envelope get ``None`` for the
busy path; raise sites that need the contender's pid read ``result.fd``
on success and ``result.holder_pid`` / ``result.holder_started_at`` on
the busy branch.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Literal

from lies.utils.lock_heartbeat import (
    AcquireResult,
    read_heartbeat,
    read_owner_pid,
)

_log = logging.getLogger(__name__)

MAX_FLOCK_AGE_S = 2 * 3600  # 2h ceiling on memory flock liveness


def acquire_create_lock(  # type: ignore[no-untyped-def]
    path: Path,
    *,
    max_age_s: float,
    pid_path: Path | None = None,
    state_json_path: Path | None = None,
    pid_alive_fn=None,
    force_repair: bool = False,
) -> AcquireResult | None:
    """Atomically create ``path``. Return ``AcquireResult`` on win, ``None`` on contention.

    Optional ``pid_path`` + ``state_json_path``: if provided, treat an
    existing create-lock as a stale holder when its stored PID is
    dead (or its heartbeat is older than ``max_age_s``) and reap + retry
    once. See the module docstring for the exception policy.

    On a contended-but-live branch with the envelope supplied, the
    return is ``AcquireResult(fd=-1, holder_pid=<read from pid file>,
    holder_started_at=<read from heartbeat>, status="busy")`` so raise
    sites can surface operator-actionable messages. Callers that omit
    the envelope still receive ``None`` for the busy path (legacy
    semantics).

    ``force_repair=True`` escalates the second-chance reap: when the
    envelope is held by what looks like a live contender, unconditionally
    reap + retry once. The caller (``_acquire_wiki_flock``) surfaces
    ``WikiFlockUnrepairable`` if the retry still loses; without
    ``force_repair``, a live holder always wins (returns an
    ``AcquireResult`` with ``status="busy"`` when the envelope was
    supplied, otherwise ``None``).

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

    def _wrap(fd: int, status: Literal["acquired", "dead_reaped"] = "acquired") -> AcquireResult:
        return AcquireResult(fd=fd, status=status)

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        return _wrap(os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
    except FileExistsError:
        if pid_path is not None and state_json_path is not None:
            if force_repair:
                # Unconditional reap + retry once. The caller will
                # raise WikiFlockUnrepairable if the retry still loses.
                _reap_files(path, pid_path, state_json_path)
            elif not _reap_if_stale(path, pid_path, state_json_path, max_age_s, pid_alive_fn):
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
            # Reap removed the create-lock; retry the create once.
            try:
                return _wrap(
                    os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644),
                    status="dead_reaped",
                )
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
            return _wrap(os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
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
    try:
        stored_pid = int(pid_path.read_text(encoding="utf-8").strip())
    except OSError, ValueError:
        _log.warning("could not parse pid file %s", pid_path)
        return False

    if stored_pid == os.getpid():
        # Self-acquire: not contended. Reap the stale lock and let retry succeed.
        _reap_files(create_lock, pid_path, state_json_path)
        return True

    # pid_alive_fn may raise on EPERM; classifier already handles that.
    if pid_alive_fn(stored_pid):
        # Holder alive. Check wall-clock window.
        if state_json_path.exists():
            try:
                payload = json.loads(state_json_path.read_text(encoding="utf-8"))
                started_at = float(payload.get("started_at", 0.0))
            except OSError, ValueError, json.JSONDecodeError:
                started_at = 0.0
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
