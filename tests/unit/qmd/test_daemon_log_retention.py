"""A daemon that dies must leave a log behind.

qmd truncates ``mcp.log`` on every daemon start. That is qmd's
behaviour and LIES cannot change it, but it means the one artefact
that could explain an unexpected death is destroyed by the very act
of restarting the daemon -- and LIES restarts daemons routinely, as
the seam's recycle path.

The cost is concrete. On 2026-10-03 the machine-global qmd daemon
stopped sometime between 23:02Z and 06:35Z. Every candidate cause was
ruled out by measurement (no OOM record; the staleness marker older
than the pidfile; the tag-filter CUDA abort followed by a live daemon
15 minutes later; no WSL restart -- ``/init`` up since Oct 2). What
remained was a question with no answer available, because the dead
daemon's log had been truncated and ``mcp.pid`` only ever holds the
current pid.

So the log is copied aside before each stop, with a bounded number
of generations kept. Best-effort: preserving a diagnostic must never
be the reason a stop fails.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from lies.qmd import lifecycle


def _seed_log(cache: Path, text: str = "line one\nline two\n") -> Path:
    log = cache / "qmd" / "mcp.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(text)
    return log


def test_stopping_the_daemon_preserves_its_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`_down` copies the log before it stops anything.

    The copy has to happen *before* the stop, because the stop is
    what lets qmd truncate on the next start.
    """
    cache = tmp_path / "cache"
    _seed_log(cache, "expanding query 3/7\nCUDA error\n")

    monkeypatch.setattr(lifecycle, "_logfile", lambda: cache / "qmd" / "mcp.log")
    monkeypatch.setattr(lifecycle, "_read_pid", lambda: 4242)
    monkeypatch.setattr(lifecycle, "_port_listening", lambda port: True)
    monkeypatch.setattr(lifecycle, "_find_qmd", lambda: "qmd")
    monkeypatch.setattr(
        "lies.qmd._subprocess._run_qmd",
        lambda *a, **k: None,
    )

    lifecycle._down()

    retained = sorted((cache / "qmd").glob("mcp.log.*"))
    assert retained, f"no preserved log in {sorted((cache / 'qmd').iterdir())}"
    assert "expanding query 3/7" in retained[-1].read_text(), (
        "the preserved copy must hold the daemon's own output, not an empty file"
    )


def test_preservation_keeps_a_bounded_number_of_generations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Unbounded retention trades one disk leak for the problem fixed.

    Every stop adds a generation. Without a bound, a daemon recycled
    weekly for a year leaves 52 files nobody prunes, in the same
    cache directory qmd owns.
    """
    cache = tmp_path / "cache"
    monkeypatch.setattr(lifecycle, "_logfile", lambda: cache / "qmd" / "mcp.log")
    monkeypatch.setattr(lifecycle, "_read_pid", lambda: 4242)
    monkeypatch.setattr(lifecycle, "_port_listening", lambda port: True)
    monkeypatch.setattr(lifecycle, "_find_qmd", lambda: "qmd")
    monkeypatch.setattr("lies.qmd._subprocess._run_qmd", lambda *a, **k: None)

    for i in range(lifecycle.LOG_GENERATIONS_KEPT + 4):
        _seed_log(cache, f"generation {i}\n")
        lifecycle._down()
        time.sleep(0.001)

    retained = sorted((cache / "qmd").glob("mcp.log.*"))
    assert len(retained) <= lifecycle.LOG_GENERATIONS_KEPT, (
        f"{len(retained)} generations kept; the bound is {lifecycle.LOG_GENERATIONS_KEPT}"
    )


def test_preservation_failure_never_blocks_the_stop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A diagnostic that cannot be written must not stop a daemon.

    The stop is the operation with a purpose. Preserving the log is
    bookkeeping, and bookkeeping that can fail the main operation is
    worse than the log it was protecting.
    """
    cache = tmp_path / "cache"
    _seed_log(cache)

    monkeypatch.setattr(lifecycle, "_logfile", lambda: cache / "qmd" / "mcp.log")
    monkeypatch.setattr(lifecycle, "_read_pid", lambda: 4242)
    monkeypatch.setattr(lifecycle, "_port_listening", lambda port: True)
    monkeypatch.setattr(lifecycle, "_find_qmd", lambda: "qmd")
    monkeypatch.setattr(
        lifecycle,
        "_preserve_daemon_log",
        lambda: (_ for _ in ()).throw(OSError("read-only filesystem")),
    )
    stopped: list[bool] = []
    monkeypatch.setattr(
        "lies.qmd._subprocess._run_qmd",
        lambda *a, **k: stopped.append(True),
    )

    lifecycle._down()  # must not raise

    assert stopped == [True], "the stop still ran"


def test_preserving_a_missing_log_is_not_an_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No log file means nothing to preserve, which is a normal state."""
    cache = tmp_path / "cache"
    monkeypatch.setattr(lifecycle, "_logfile", lambda: cache / "qmd" / "mcp.log")
    lifecycle._preserve_daemon_log()  # must not raise
