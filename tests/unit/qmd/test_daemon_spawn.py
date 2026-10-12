"""``_spawn_qmd_daemon``'s argv and environment."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from lies.qmd import daemon as qmd_daemon


@pytest.fixture
def captured_spawn(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture what ``_run_qmd`` is asked to run, and refuse to run it."""
    calls: list[dict[str, Any]] = []

    def fake_run_qmd(args: list[str], **kw: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append({"args": args, **kw})
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(qmd_daemon, "_run_qmd", fake_run_qmd)
    return calls


def test_the_default_spawn_is_byte_identical_to_before(
    captured_spawn: list[dict[str, Any]],
) -> None:
    """No port, no data dir: today's argv and today's environment."""
    qmd_daemon._spawn_qmd_daemon()

    assert len(captured_spawn) == 1
    assert captured_spawn[0]["args"] == ["qmd", "mcp", "--http", "--daemon"]
    assert not captured_spawn[0]["env_overrides"], (
        "the default spawn must not set any environment override"
    )
    assert captured_spawn[0]["cwd"] == Path.cwd()
    assert captured_spawn[0]["timeout"] == qmd_daemon.STATUS_TIMEOUT_S


def test_a_port_is_passed_with_the_flag_qmd_actually_reads(
    captured_spawn: list[dict[str, Any]], tmp_path: Path
) -> None:
    """``--port N`` is appended, and the data dir rides as an env var."""
    qmd_daemon._spawn_qmd_daemon(port=9999, data_dir=tmp_path / "cache")

    call = captured_spawn[0]
    assert call["args"] == [
        "qmd",
        "mcp",
        "--http",
        "--daemon",
        "--port",
        "9999",
    ]
    assert call["env_overrides"] == {"XDG_CACHE_HOME": str(tmp_path / "cache")}


def test_port_alone_is_allowed_and_is_what_production_needs(
    captured_spawn: list[dict[str, Any]],
) -> None:
    """A port with no ``data_dir`` — sharing the real cache root — works."""
    # The single call production needs: restart THE daemon, on ITS
    # port, sharing ITS cache root.
    qmd_daemon._spawn_qmd_daemon(port=9999)

    call = captured_spawn[0]
    assert call["args"] == ["qmd", "mcp", "--http", "--daemon", "--port", "9999"]
    assert call["env_overrides"] == {}, "the real cache root is inherited"


def test_da_dir_alone_is_still_allowed(
    captured_spawn: list[dict[str, Any]], tmp_path: Path
) -> None:
    """``data_dir`` alone remains valid — it is the throwaway's isolation."""
    qmd_daemon._spawn_qmd_daemon(data_dir=tmp_path)

    assert captured_spawn[0]["args"] == ["qmd", "mcp", "--http", "--daemon"]
    assert captured_spawn[0]["env_overrides"] == {"XDG_CACHE_HOME": str(tmp_path)}


def test_a_successful_spawn_returns_the_result(
    captured_spawn: list[dict[str, Any]],
) -> None:
    """The CompletedProcess comes back so callers can read qmd's stdout."""
    returned = qmd_daemon._spawn_qmd_daemon()

    assert returned is not None
    assert returned.returncode == 0


def test_raise_on_error_gives_the_caller_the_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``raise_on_error`` is how ``ensure_qmd_daemon`` names the failure."""

    def boom(args: list[str], **kw: Any) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(cmd=args, timeout=15.0)

    monkeypatch.setattr(qmd_daemon, "_run_qmd", boom)

    assert qmd_daemon._spawn_qmd_daemon() is None  # default swallows
    with pytest.raises(subprocess.TimeoutExpired):
        qmd_daemon._spawn_qmd_daemon(raise_on_error=True)


def test_a_failing_spawn_is_still_swallowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Timeout and OSError keep their blanket swallow."""

    def boom(args: list[str], **kw: Any) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(cmd=args, timeout=1.0)

    monkeypatch.setattr(qmd_daemon, "_run_qmd", boom)
    qmd_daemon._spawn_qmd_daemon(port=9999, data_dir=tmp_path)

    def boom_oserror(args: list[str], **kw: Any) -> subprocess.CompletedProcess[bytes]:
        raise OSError("no qmd")

    monkeypatch.setattr(qmd_daemon, "_run_qmd", boom_oserror)
    qmd_daemon._spawn_qmd_daemon(port=9999, data_dir=tmp_path)


def test_env_overrides_are_merged_not_substituted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spawn override cannot drop the forced ``NO_COLOR``."""
    from lies.qmd import _subprocess

    seen: list[dict[str, str]] = []

    class _FakePopen:
        def __init__(self, args: Any, **kw: Any) -> None:
            seen.append(kw["env"])

        def __enter__(self) -> "_FakePopen":
            return self

        def __exit__(self, *a: Any) -> None:
            return None

        @property
        def stdout(self) -> Any:
            raise AssertionError("unreachable")

    monkeypatch.setattr(_subprocess.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(_subprocess.os, "open", lambda *a, **kw: 0)

    with pytest.raises(Exception):  # noqa: B017 - the fake cannot complete a read
        _subprocess._run_qmd(
            ["qmd", "status"],
            cwd=Path.cwd(),
            timeout=1.0,
            env_overrides={"XDG_CACHE_HOME": "/tmp/throwaway"},
        )

    assert seen and seen[0]["NO_COLOR"] == "1"
    assert seen[0]["XDG_CACHE_HOME"] == "/tmp/throwaway"
    assert "PATH" in seen[0], "the operator's environment is inherited, not replaced"
