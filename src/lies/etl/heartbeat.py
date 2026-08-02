"""Cross-process sync heartbeat for busy detection.

A single sync run writes ``<wiki>/.lies/sync.lock`` with its PID,
start time, and collection. A subsequent run reads the file and
treats it as busy unless the PID is dead or the heartbeat is older
than ``MAX_SYNC_AGE_S`` (stale-recovery for crashes).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

MAX_SYNC_AGE_S = 3600
_HEARTBEAT_NAME = "sync.lock"


@dataclass(frozen=True)
class Heartbeat:
    pid: int
    started_at: float
    collection: str


def _heartbeat_path(wiki_root: Path) -> Path:
    return wiki_root / ".lies" / _HEARTBEAT_NAME


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def write_heartbeat(wiki_root: Path, heartbeat: Heartbeat) -> None:
    p = _heartbeat_path(wiki_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": heartbeat.pid,
        "started_at": heartbeat.started_at,
        "collection": heartbeat.collection,
    }
    p.write_text(json.dumps(payload), encoding="utf-8")


def read_heartbeat(wiki_root: Path) -> Heartbeat | None:
    p = _heartbeat_path(wiki_root)
    if not p.exists():
        return None
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return Heartbeat(
        pid=int(payload["pid"]),
        started_at=float(payload["started_at"]),
        collection=str(payload["collection"]),
    )


def clear_heartbeat(wiki_root: Path) -> None:
    p = _heartbeat_path(wiki_root)
    if p.exists():
        p.unlink()


def heartbeat_is_stale(heartbeat: Heartbeat) -> bool:
    if not pid_alive(heartbeat.pid):
        return True
    age = time.time() - heartbeat.started_at
    return age > MAX_SYNC_AGE_S


def wait_until_free(wiki_root: Path, *, poll_interval_s: float = 1.0) -> None:
    while True:
        h = read_heartbeat(wiki_root)
        if h is None or heartbeat_is_stale(h):
            return
        time.sleep(poll_interval_s)
