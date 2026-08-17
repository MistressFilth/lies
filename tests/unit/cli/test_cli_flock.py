"""Tests for the ``lies flock`` subcommand."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lies import xdg
from lies.cli import app
from lies.utils.lock_heartbeat import Heartbeat, write_heartbeat, write_owner_pid

runner = CliRunner()


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in [
        "LIES_XDG_DATA_HOME",
        "LIES_XDG_CONFIG_HOME",
        "LIES_XDG_CACHE_HOME",
        "LIES_XDG_STATE_HOME",
        "LIES_XDG_RUNTIME_DIR",
        "XDG_DATA_HOME",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "XDG_RUNTIME_DIR",
    ]:
        monkeypatch.delenv(k, raising=False)


def _combined(result) -> str:
    """Click 8.2+ splits stderr from ``.output``; tolerate either layout."""
    return (result.stdout or "") + (result.stderr or "")


def _seed_wiki(tmp_path: Path, name: str) -> None:
    """Create the wiki's runtime dir so ``xdg.runtime_dir_for`` lands on disk."""
    runtime_root = xdg.runtime_dir_for(name)
    runtime_root.mkdir(parents=True, exist_ok=True)


def _seed_flock(
    tmp_path: Path,
    name: str,
    pid: int,
    *,
    fresh: bool = True,
) -> None:
    """Plant the four memory-flock files in the wiki's runtime dir.

    ``fresh=True`` (default) sets ``started_at`` to ``time.time()`` so the
    heartbeat looks alive and well within the recovery window. ``fresh=False``
    sets ``started_at`` to 3h ago so the heartbeat is stale.
    """
    runtime_root = xdg.runtime_dir_for(name)
    runtime_root.mkdir(parents=True, exist_ok=True)
    lock = runtime_root / "memory.lock"
    create_lock = runtime_root / "memory.lock.create"
    pid_path = runtime_root / "memory.pid"
    state_path = runtime_root / "memory.state.json"

    lock.write_text("", encoding="utf-8")
    create_lock.write_text("", encoding="utf-8")
    write_owner_pid(pid_path, pid)
    started_at = time.time() if fresh else time.time() - (3 * 3600)
    write_heartbeat(state_path, Heartbeat(pid=pid, started_at=started_at, scope=name))


def test_lies_flock_status_reports_held(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    _seed_wiki(tmp_path, "mywiki")
    # 999999 is reliably dead (pid_alive returns False); status reports
    # the flock as ``held`` because the heartbeat is fresh and pid_alive
    # is not consulted by the read-only status command.
    _seed_flock(tmp_path, "mywiki", pid=999999)

    result = runner.invoke(app, ["flock", "mywiki", "status"])
    assert result.exit_code == 0, _combined(result)
    assert "held" in _combined(result)


def test_lies_flock_status_emits_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    _seed_wiki(tmp_path, "mywiki")
    _seed_flock(tmp_path, "mywiki", pid=4242)

    result = runner.invoke(app, ["flock", "mywiki", "status", "--json"])
    assert result.exit_code == 0, _combined(result)
    payload = json.loads(result.stdout)
    assert payload["status"] == "held"
    assert payload["pid"] == 4242
    assert payload["wiki"] == "mywiki"


def test_lies_flock_status_returns_2_when_no_flock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    _seed_wiki(tmp_path, "absent")

    result = runner.invoke(app, ["flock", "absent", "status"])
    assert result.exit_code == 2
    assert "no flock" in _combined(result).lower()


def test_lies_flock_status_returns_1_when_stale(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A held flock with a stale heartbeat exits 1 (caller can act on it).

    Mirrors ``test_lies_flock_status_reports_held`` but seeds
    ``started_at`` 10h ago so the heartbeat falls outside the
    ``MAX_FLOCK_AGE_S`` window — ``flock_status`` reports
    ``status: stale`` and exits 1, not 0.
    """
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    _seed_wiki(tmp_path, "mywiki")
    # Seed a stale flock: pid=999999 (dead) + started_at 10h ago.
    _seed_flock(tmp_path, "mywiki", pid=999999, fresh=False)

    result = runner.invoke(app, ["flock", "mywiki", "status"])
    assert result.exit_code == 1, _combined(result)
    combined = _combined(result)
    assert "stale" in combined.lower()


def test_lies_flock_status_json_returns_1_when_stale(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """JSON variant mirrors the text exit: 1 for a stale candidate, 2 for absent."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    _seed_wiki(tmp_path, "mywiki")
    _seed_flock(tmp_path, "mywiki", pid=999999, fresh=False)

    result = runner.invoke(app, ["flock", "mywiki", "status", "--json"])
    assert result.exit_code == 1, _combined(result)
    payload = json.loads(result.stdout)
    assert payload["status"] == "stale"
    assert payload["fresh"] is False
    assert ".lock" not in payload["files"]


def test_lies_flock_status_json_absent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    _seed_wiki(tmp_path, "absent")

    result = runner.invoke(app, ["flock", "absent", "status", "--json"])
    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload == {"status": "absent"}


def test_lies_flock_force_repair_happy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    _seed_wiki(tmp_path, "mywiki")
    # Seed a stale flock: pid=999999 (dead) + started_at 3h ago.
    _seed_flock(tmp_path, "mywiki", pid=999999, fresh=False)

    result = runner.invoke(app, ["flock", "mywiki", "force-repair"])
    assert result.exit_code == 0, _combined(result)
    combined = _combined(result)
    assert "reap" in combined
    assert "ok" in combined.lower()


def test_lies_flock_force_repair_unrepairable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    _seed_wiki(tmp_path, "mywiki")
    _seed_flock(tmp_path, "mywiki", pid=999999)

    # Simulate a live contender that recreates the create-lock between
    # force-repair's reap and its retry, so ``acquire_create_lock`` returns
    # ``None`` (O_EXCL fails because the path exists with current mtime).
    import lies.cli as cli_module

    def fail_acquire(path, *, max_age_s, pid_path=None, state_json_path=None, pid_alive_fn=None):
        path.touch()

    monkeypatch.setattr(cli_module, "acquire_create_lock", fail_acquire)

    result = runner.invoke(app, ["flock", "mywiki", "force-repair"])
    assert result.exit_code != 0
    combined = _combined(result)
    assert "unrepairable" in combined.lower()


@pytest.mark.xfail(
    reason=(
        "intentionally RED at Task 4 commit point: Task 5 implements the "
        "capture-before-reap body in flock_force_repair; until then the "
        "Unrepairable message omits the captured pid. Remove this xfail "
        "marker when Task 5 lands."
    ),
    strict=True,
)
def test_flock_force_repair_unrepairable_message_cites_captured_pid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``lies flock <name> force-repair`` captures pid + heartbeat BEFORE
    its reap loop; even when the post-reap retry still loses, the
    operator-actionable message cites the captured pid + start time.

    NOTE: This test is intentionally RED at the Task 4 commit point.
    Task 5 (commit on the same branch) implements the capture-before-reap
    body in ``flock_force_repair``; until that lands, the current
    implementation raises ``WikiFlockUnrepairable`` with a message that
    omits the captured pid. The test pins the spec-required behavior so
    Task 5 must satisfy it.

    Sibling tests in this file use ``pid=999999`` as the "reliably dead"
    sentinel — ``Task 5``'s capture-before-reap reads pid_file contents
    verbatim, so pid liveness is irrelevant to message construction.
    ``strict=True`` makes Task 5's GREEN transition a hard XPASS that
    the reviewer must remove; ``strict=False`` would silently absorb
    the XPASS into the green run.
    """
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    _seed_wiki(tmp_path, "mywiki")
    # 999999 is reliably dead (pid_alive returns False); Task 5's
    # capture-before-reap reads pid_file contents verbatim, so the
    # captured-pid message is host-independent.
    _seed_flock(tmp_path, "mywiki", pid=999999)

    # Force the post-reap retry to fail: ``acquire_create_lock`` returns
    # ``None`` so ``flock_force_repair`` raises ``WikiFlockUnrepairable``.
    # Type the signature to match ``acquire_create_lock`` (same shape as
    # the sibling ``test_lies_flock_force_repair_unrepairable`` above)
    # so a Task 5 call-signature change surfaces as a TypeError here
    # rather than going undetected.
    import lies.cli as cli_module

    def fail_acquire(path, *, max_age_s, pid_path=None, state_json_path=None, pid_alive_fn=None):
        return None

    monkeypatch.setattr(cli_module, "acquire_create_lock", fail_acquire)

    result = runner.invoke(app, ["flock", "mywiki", "force-repair"])
    assert result.exit_code != 0
    # Click 8.2+ splits stderr from ``.output``; ``_combined`` covers both.
    out = _combined(result)
    assert "pid 999999" in out
    assert "lies flock mywiki force-repair" in out
