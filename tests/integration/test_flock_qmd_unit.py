"""Operator CLI for the site-wide qmd flock."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lies.cli.operator import flock_app


@pytest.fixture
def qmd_lock_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Redirect the qmd lock path to ``tmp_path``.

    The lock module no longer pre-creates a module-level triad
    at import time (M-6); production acquire/release threads its
    own paths through ``_lock_paths()``. The fixture sets the
    env var and resolves the triad once for this test.
    """
    monkeypatch.setenv("LIES_QMD_LOCK_PATH", str(tmp_path / "qmd.lock"))
    from lies.qmd import lock as lock_mod  # type: ignore[import-not-found]

    lock_path, pid_path, state_path = lock_mod._lock_paths()
    return {
        "lock": lock_path,
        "pid": pid_path,
        "state": state_path,
    }


def test_flock_qmd_status_no_holder(qmd_lock_paths: dict[str, Path]) -> None:
    """`lies flock qmd status` with no lock present prints 'no flock held' and exits 2.

    Exit code 2 matches the existing ``flock <name> status`` absent
    convention so shell callers can branch on it without parsing text.
    """
    runner = CliRunner()
    result = runner.invoke(flock_app, ["qmd", "status"])
    assert result.exit_code == 2, (result.stdout or "") + (result.stderr or "")
    assert "no flock held" in (result.stdout or "").lower()


def test_flock_qmd_status_with_holder(qmd_lock_paths: dict[str, Path]) -> None:
    """`lies flock qmd status` with a live holder prints the holder pid."""
    import lies.qmd.lock as lock_mod  # type: ignore[import-not-found]

    fd = lock_mod._acquire_with_poll(retry_budget_s=2.0, max_age_s=60.0)
    try:
        runner = CliRunner()
        result = runner.invoke(flock_app, ["qmd", "status"])
        assert result.exit_code == 0, (result.stdout or "") + (result.stderr or "")
        assert str(os.getpid()) in (result.stdout or "")
    finally:
        lock_mod._release(fd)


def test_flock_qmd_force_repair_clears_stale_holders(qmd_lock_paths: dict[str, Path]) -> None:
    """A pre-existing create-lock + dead pid file is reaped before the next acquire succeeds."""
    paths = qmd_lock_paths
    paths["lock"].parent.mkdir(parents=True, exist_ok=True)
    paths["lock"].touch()
    paths["pid"].write_text("999999", encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(flock_app, ["qmd", "force-repair"])
    assert result.exit_code == 0, (result.stdout or "") + (result.stderr or "")
    # After force-repair, pid file and create-lock are gone.
    assert not paths["lock"].exists()
    assert not paths["pid"].exists()
