"""``_spawn_qmd_daemon``'s argv and environment.

What the flags are is not guesswork. Verified against qmd 2.5.3 as
installed on this host (``@tobilu/qmd@2.5.3``), read from
``dist/cli/qmd.js``:

- ``--port`` is real and honoured — ``case "mcp":`` reads
  ``const port = Number(cli.values.port) || 8181;`` (line 4005) and
  spawns the child with ``mcp --http --port <port>``.
- **There is no ``--data-dir`` flag.** The ``mcp`` block reads only
  ``cli.args[0]``, ``values.http``, ``values.port``, ``values.daemon``
  and ``values.index``. Nothing else.
- qmd resolves its cache dir from ``XDG_CACHE_HOME`` alone
  (``dist/cli/qmd.js:3981-3983``; ``dist/store.js:428-434``), covering
  ``index.sqlite``, ``mcp.pid`` **and** ``mcp.log``. So
  ``XDG_CACHE_HOME`` is the lever, and it appends its own ``qmd/``
  segment — passing ``X`` yields ``X/qmd/``.

These tests assert the argv and env LIES builds. They do not spawn;
``tests/integration/test_qmd_spawn_throwaway.py`` does that.
"""

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
    """No port, no data dir: today's argv and today's environment.

    The hard constraint on this item — a non-default spawn must not
    perturb production — is only meaningful if the default is pinned.
    """
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
    """``--port N`` is appended, and the data dir rides as an env var.

    Flag spelling pinned to qmd's own parser: ``cli.values.port``.
    """
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


def test_port_alone_is_refused(captured_spawn: list[dict[str, Any]]) -> None:
    """A spare port without its own cache dir would take over the real pidfile.

    qmd keys ``mcp.pid`` to ``XDG_CACHE_HOME`` and **not** to the port.
    A throwaway sharing the real daemon's cache dir overwrites the real
    daemon's pidfile, and both ``qmd_daemon_state`` and
    ``_reap_qmd_daemon`` read exactly that file — so the reap would
    SIGTERM the throwaway and ``qmd status`` would report the throwaway
    as *the* daemon. Refused loudly rather than allowed; the damage
    would be to a machine-global process this function does not own.
    """
    with pytest.raises(ValueError, match="mcp.pid"):
        qmd_daemon._spawn_qmd_daemon(port=9999)

    assert captured_spawn == [], "a refused spawn must not shell out at all"


def test_the_refusal_survives_a_spawn_that_would_otherwise_be_swallowed(
    captured_spawn: list[dict[str, Any]], tmp_path: Path
) -> None:
    """``data_dir`` alone is allowed; only the dangerous pairing is refused.

    The function's long-standing "never raises" contract is about
    *spawn* failures — a timeout, a missing binary — and stays that way.
    Argument misuse is a different thing and is the caller's bug.
    """
    qmd_daemon._spawn_qmd_daemon(data_dir=tmp_path)

    assert captured_spawn[0]["args"] == ["qmd", "mcp", "--http", "--daemon"]
    assert captured_spawn[0]["env_overrides"] == {"XDG_CACHE_HOME": str(tmp_path)}


def test_a_failing_spawn_is_still_swallowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Timeout and OSError keep their blanket ``pass``.

    Pins that adding arguments did not widen the exception handling.
    """

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
    """A spawn override cannot drop the forced ``NO_COLOR``.

    ``_run_qmd`` layers overrides onto ``_child_env()``; it must not
    replace it, or a daemon started against a throwaway index could come
    back with colour codes LIES then has to parse around.
    """
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
