"""Spawns a real throwaway qmd daemon on a spare port and its own index."""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import time
from pathlib import Path

import pytest

from lies.qmd import daemon as qmd_daemon

pytestmark = pytest.mark.slow


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_listening(port: int, timeout: float = 45.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.25)
    return False


def _real_pidfile() -> Path:
    raw = os.environ.get("XDG_CACHE_HOME")
    cache = Path(raw) if raw else Path.home() / ".cache"
    return cache / "qmd" / "mcp.pid"


def test_a_throwaway_daemon_serves_on_its_own_port_and_index(tmp_path: Path) -> None:
    """``_spawn_qmd_daemon(port=..., data_dir=...)`` really does isolate."""
    if not qmd_daemon.qmd_installed():  # pragma: no cover - environment-dependent
        pytest.skip("qmd is not installed")

    real_pidfile = _real_pidfile()
    real_before = real_pidfile.read_text().strip() if real_pidfile.exists() else None

    port = _free_port()
    data_dir = tmp_path / "throwaway-cache"
    spawned_pid: int | None = None

    try:
        qmd_daemon._spawn_qmd_daemon(port=port, data_dir=data_dir)

        # qmd appends its own "qmd/" to XDG_CACHE_HOME, so the pidfile
        # lands there and *only* there.
        throwaway_pidfile = data_dir / "qmd" / "mcp.pid"
        assert throwaway_pidfile.exists(), (
            f"expected qmd's pidfile at {throwaway_pidfile}; "
            f"cache dir holds {sorted(p.name for p in (data_dir / 'qmd').glob('*')) if (data_dir / 'qmd').exists() else 'nothing'}"
        )
        spawned_pid = int(throwaway_pidfile.read_text().strip())

        assert _wait_listening(port), f"nothing listening on the spare port {port}"

        async def list_tool_names() -> list[str]:
            import fastmcp

            async with fastmcp.Client(f"http://127.0.0.1:{port}/mcp") as client:
                return sorted(t.name for t in await client.list_tools())

        # The daemon serves a real MCP session, which is what the
        # recycle's readiness probe checks.
        assert asyncio.run(list_tool_names()) == [
            "get",
            "multi_get",
            "query",
            "status",
        ]

        # Its index is its own.
        assert (data_dir / "qmd" / "index.sqlite").exists(), (
            "XDG_CACHE_HOME did not relocate the index; the throwaway is "
            "sharing the machine daemon's"
        )

        # The real daemon's pidfile was never touched.
        real_after = real_pidfile.read_text().strip() if real_pidfile.exists() else None
        assert real_after == real_before, (
            "the throwaway overwrote the machine daemon's mcp.pid — "
            "_reap_qmd_daemon would now kill the wrong process"
        )
    finally:
        _stop_daemons([spawned_pid] if spawned_pid is not None else [], data_dir)
        shutil.rmtree(data_dir, ignore_errors=True)

    real_final = real_pidfile.read_text().strip() if real_pidfile.exists() else None
    assert real_final == real_before, "the machine daemon's pidfile changed"


def test_a_recycle_against_a_non_default_port_restarts_on_that_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: reap, respawn on the probed port, probe green."""
    if not qmd_daemon.qmd_installed():  # pragma: no cover - environment-dependent
        pytest.skip("qmd is not installed")

    port = _free_port()
    data_dir = tmp_path / "cache"
    monkeypatch.setattr(qmd_daemon, "SIDECAR_PATH", tmp_path / "mcp.data-dir")
    # The production shape: no --data-dir, so the spawned daemon shares
    # the cache root it was reaped from. Only the root itself is a
    # throwaway, which is what keeps the machine's pidfile out of it.
    monkeypatch.setenv("XDG_CACHE_HOME", str(data_dir))

    spawned_pids: list[int] = []
    try:
        # Seed a first daemon, exactly as the recycle's own reap would
        # find one, so there is something on `port` to restart.
        qmd_daemon._spawn_qmd_daemon(port=port)
        assert _wait_listening(port), f"no throwaway on {port} to recycle"

        pidfile = data_dir / "qmd" / "mcp.pid"
        assert pidfile.exists()
        spawned_pids.append(int(pidfile.read_text().strip()))
        first_pid = spawned_pids[0]

        state = asyncio.run(
            qmd_daemon.recycle_qmd_daemon(
                scope=tmp_path / "scope",
                daemon_url=f"http://127.0.0.1:{port}/mcp",
                ready_timeout=60.0,
            )
        )
        assert state.running is True

        # The replacement is on the probed port, and it is a new process.
        assert _wait_listening(port)
        second_pid = int(pidfile.read_text().strip())
        spawned_pids.append(second_pid)
        assert second_pid != first_pid, "the recycle did not actually restart"
        assert _port_owned_by(second_pid, port), (
            f"pid {second_pid} (the pidfile's daemon) is not listening on "
            f"{port}; the spawn landed on another port — a stray on qmd's "
            "default would own the pidfile and the probe would have failed"
        )
    finally:
        _stop_daemons(spawned_pids, data_dir)
        shutil.rmtree(data_dir, ignore_errors=True)


def _port_listening(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.3):
            return True
    except OSError:
        return False


def _port_owned_by(pid: int, port: int, host: str = "127.0.0.1") -> bool:
    """Is ``pid`` the process listening on ``port``?"""
    wanted = f"socket:[{_socket_inode(host, port)}]"
    try:
        for fd in os.listdir(f"/proc/{pid}/fd"):
            try:
                if os.readlink(f"/proc/{pid}/fd/{fd}") == wanted:
                    return True
            except OSError:
                continue
    except OSError:
        return False
    return False


def _socket_inode(host: str, port: int) -> str | None:
    """The inode of the LISTEN socket on ``(host, port)``, from /proc/net/tcp."""
    want_port = f"{port:04X}"
    for name in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(name) as fh:
                lines = fh.read().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 10 or fields[3] != "0A":  # 0A == TCP_LISTEN
                continue
            if fields[1].rsplit(":", 1)[-1] != want_port:
                continue
            return fields[9]
    return None


def test_daemon_tool_stale_path_respawns_on_the_configured_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The third spawn site, end to end, against a real qmd binary."""
    if not qmd_daemon.qmd_installed():  # pragma: no cover - environment-dependent
        pytest.skip("qmd is not installed")

    from lies.qmd import access

    port = _free_port()
    data_dir = tmp_path / "cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(data_dir))
    monkeypatch.setenv("LIES_QMD_URL", f"http://127.0.0.1:{port}/mcp")

    # Isolate the stale respawn from the recovery that would repair it.
    async def _no_recovery(url: str, name: str, make_call: object) -> dict[str, list]:
        return {"hits": []}

    monkeypatch.setattr(access, "_call_with_recovery", _no_recovery)

    pids: list[int] = []
    try:
        qmd_daemon._spawn_qmd_daemon(port=port)
        assert _wait_listening(port), f"no throwaway on {port}"
        pidfile = data_dir / "qmd" / "mcp.pid"
        first = int(pidfile.read_text().strip())
        pids.append(first)

        # Make staleness REAL: the daemon started before this write.
        # ``_is_daemon_stale`` compares the marker against the pidfile,
        # and both live under XDG_CACHE_HOME, so the throwaway owns them.
        marker = data_dir / "qmd" / "last-write-marker"
        marker.write_text("x")
        assert qmd_daemon._is_daemon_stale(), (
            "could not make the throwaway genuinely stale; this test would "
            "otherwise pass without exercising the respawn at all"
        )

        result = asyncio.run(access.daemon_tool("status", {}))
        assert result == {"hits": []}

        second = int(pidfile.read_text().strip())
        pids.append(second)
        assert second != first, "the stale path did not actually respawn"
        # The spawn is fire-and-forget — `qmd mcp --http --daemon`
        # double-forks and returns — so the replacement needs a moment
        # to bind. Poll rather than reading once.
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline and not _port_owned_by(second, port):
            time.sleep(0.2)
        assert _port_owned_by(second, port), (
            f"the respawned daemon (pid {second}) is not listening on the "
            f"configured port {port}; the port was dropped at this spawn "
            f"site and the replacement bound qmd's default instead"
        )
    finally:
        _stop_daemons(pids, data_dir)
        shutil.rmtree(data_dir, ignore_errors=True)


def _stop_daemons(pids: list[int], data_dir: Path) -> None:
    """SIGTERM every pid we started, and whatever the pidfile now names."""
    known = list(pids)
    pidfile = data_dir / "qmd" / "mcp.pid"
    if pidfile.exists():
        try:
            known.append(int(pidfile.read_text().strip()))
        except ValueError:
            pass
    for pid in dict.fromkeys(known):
        try:
            os.kill(pid, 15)
        except OSError:
            pass
    for pid in dict.fromkeys(known):
        for _ in range(100):
            try:
                os.kill(pid, 0)
            except OSError:
                break
            time.sleep(0.05)


def test_a_recycle_probe_against_a_dead_port_is_the_measured_shape() -> None:
    """The exception the recycle must retry, straight from fastmcp."""
    import fastmcp
    import httpx
    import mcp

    port = _free_port()

    async def probe() -> BaseException | None:
        try:
            async with fastmcp.Client(f"http://127.0.0.1:{port}/mcp") as client:
                await client.list_tools()
        except BaseException as exc:  # noqa: BLE001 - this is the measurement
            return exc
        return None

    exc = asyncio.run(probe())
    assert exc is not None, "a closed port served a session?"
    assert isinstance(exc, RuntimeError), (
        f"fastmcp no longer presents a dead session as RuntimeError; it "
        f"raised {type(exc).__module__}.{type(exc).__qualname__}. "
        f"Re-derive _RECYCLE_PROBE_ERRORS."
    )
    cause = exc.__cause__
    assert cause is not None, "the transport error is no longer on __cause__"
    assert not isinstance(cause, (httpx.HTTPError, OSError)), (
        "the vendored httpx error is now catchable by the legacy arms; "
        "the wrapping RuntimeError may no longer be what does the work"
    )
    assert isinstance(exc, (httpx.HTTPError, mcp.MCPError, OSError, RuntimeError))
