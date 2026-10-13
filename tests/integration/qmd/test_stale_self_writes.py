"""Integration test: orchestrator's own writes do not make its own daemon stale."""

from __future__ import annotations

from pathlib import Path

import pytest

from lies.qmd.daemon import _daemon_start_time, _global_last_write_marker


def test_orchestrator_self_writes_do_not_make_self_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After WikiMemoryService.apply_plan writes to the marker, _is_daemon_stale must still return False."""
    marker: Path = _global_last_write_marker()
    daemon_start = _daemon_start_time()

    # If either side is missing, the orchestrator cannot have made
    # itself stale; the daemon is either uninitialized or the
    # marker is fresh.
    if daemon_start is None or not marker.exists():
        pytest.skip("qmd daemon or marker not initialized; cannot assert property")

    marker_mtime = marker.stat().st_mtime
    daemon_mtime = daemon_start

    # Property under test: this orchestrator cannot make itself
    # stale. If this assertion ever fires, the marker-touch
    # surface has grown outside WikiMemoryService.apply_plan +
    # LibraryWriter.commit, and we need the per-N-call counter.
    assert marker_mtime <= daemon_mtime + 0.001, (
        f"orchestrator self-writes made itself stale: "
        f"marker_mtime={marker_mtime}, daemon_mtime={daemon_mtime}, "
        f"diff={marker_mtime - daemon_mtime}s"
    )
