"""Lock heartbeat + holder-PID file helpers.

Pair with :mod:`lies.utils.exclusive` — the lock primitive leaves
"who holds the flock" and "since when" to these helpers. Heartbeat is
a flat JSON dict so future expansion (memory size, build sha, etc.)
doesn't break readers.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class Heartbeat:
    pid: int
    started_at: float
    scope: str = ""


def write_owner_pid(path: Path, pid: int) -> None:
    """Write ``pid`` as one int per line (readable without JSON)."""
    path.write_text(str(int(pid)), encoding="utf-8")


def read_owner_pid(path: Path) -> int | None:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    text = text.strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def write_heartbeat(path: Path, heartbeat: Heartbeat) -> None:
    path.write_text(json.dumps(asdict(heartbeat)), encoding="utf-8")


def read_heartbeat(path: Path) -> Heartbeat | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError, json.JSONDecodeError:
        return None
    try:
        return Heartbeat(
            pid=int(payload["pid"]),
            started_at=float(payload["started_at"]),
            scope=str(payload.get("scope", "")),
        )
    except KeyError, ValueError, TypeError:
        return None
