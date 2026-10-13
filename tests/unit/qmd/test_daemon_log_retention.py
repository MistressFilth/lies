"""A daemon that dies must leave a log behind."""

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
    """`_down` copies the log before it stops anything."""
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
    """Unbounded retention trades one disk leak for the problem fixed."""
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
    """A diagnostic that cannot be written must not stop a daemon."""
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
