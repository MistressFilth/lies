"""Spawn a real throwaway qmd daemon on a spare port and its own index.

This is the test that A3 exists for. Before this item,
``_spawn_qmd_daemon`` was hardcoded to ``qmd mcp --http --daemon``: one
port, the machine's real index, no way to point it anywhere else. A
recycle can therefore only ever restart *the* daemon, so anything that
needed a disposable daemon — including this branch's measurement of the
worst-case recycle hold — had to be driven through a stub instead of a
real process.

Everything here is isolated three ways, and all three matter:

- **Its own port**, so it cannot collide with the operator's daemon.
- **Its own ``XDG_CACHE_HOME``**, which relocates ``index.sqlite``,
  ``mcp.pid`` and ``mcp.log``. This is not incidental: qmd keys
  ``mcp.pid`` to ``XDG_CACHE_HOME`` and **not** to the port, so a spare
  port *without* a spare cache dir would overwrite the real daemon's
  pidfile — and ``qmd_daemon_state`` and ``_reap_qmd_daemon`` both read
  that file. That is why ``_spawn_qmd_daemon(port=...)`` without
  ``data_dir=`` is refused.
- **Teardown by the throwaway's own pidfile**, never ``qmd mcp stop``,
  which reads the pidfile under the *current* environment.

The real daemon is asserted untouched, before and after.
"""

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
    """``_spawn_qmd_daemon(port=..., data_dir=...)`` really does isolate.

    Skips rather than fails when qmd is absent: an operator without qmd
    cannot be told their machine is broken by this file. When qmd *is*
    present, a spawn that does not come up is a real failure.
    """
    if not qmd_daemon.qmd_installed():  # pragma: no cover - environment-dependent
        pytest.skip("qmd is not installed")

    real_pidfile = _real_pidfile()
    real_before = real_pidfile.read_text().strip() if real_pidfile.exists() else None

    port = _free_port()
    data_dir = tmp_path / "throwaway-cache"
    spawned_pid: int | None = None

    try:
        qmd_daemon._spawn_qmd_daemon(port=port, data_dir=data_dir)

        # qmd appends its own "qmd/" to XDG_CACHE_HOME — see the module
        # docstring. The pidfile lands there, and *only* there.
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
        if spawned_pid is not None:
            try:
                os.kill(spawned_pid, 15)
            except OSError:
                pass
            for _ in range(100):
                try:
                    os.kill(spawned_pid, 0)
                except OSError:
                    break
                time.sleep(0.05)
        shutil.rmtree(data_dir, ignore_errors=True)

    real_final = real_pidfile.read_text().strip() if real_pidfile.exists() else None
    assert real_final == real_before, "the machine daemon's pidfile changed"


def test_a_recycle_against_a_non_default_port_restarts_on_that_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: reap, respawn on the probed port, probe green.

    This is the bug the ``port=`` refusal used to cause, run against a
    real qmd binary rather than a stub. With ``LIES_QMD_URL`` pointing at
    a non-8181 daemon:

    - the reap correctly kills the daemon on **that** port;
    - a spawn without ``--port`` brings up a **stray on 8181**, which
      also overwrites ``mcp.pid``;
    - the probe then polls the configured port for its whole budget and
      raises ``QmdRecycleFailed``.

    The operator ends up having lost their daemon and gained a squatting
    one, and ``qmd.js:4008-4013`` then refuses to start a real daemon
    because the stray's pidfile is live.

    Isolation: a throwaway ``XDG_CACHE_HOME`` so the pidfile this writes
    is not the machine's, and the sidecar redirected so the recycle does
    not rewrite the operator's real one. Everything else — ``_reap_qmd_daemon``,
    ``_spawn_qmd_daemon``, the real probe — is the production path.
    """
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
        for pid in dict.fromkeys(spawned_pids):
            try:
                os.kill(pid, 15)
            except OSError:
                pass
        for pid in dict.fromkeys(spawned_pids):
            for _ in range(100):
                try:
                    os.kill(pid, 0)
                except OSError:
                    break
                time.sleep(0.05)
        shutil.rmtree(data_dir, ignore_errors=True)


def _port_listening(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.3):
            return True
    except OSError:
        return False


def _port_owned_by(pid: int, port: int, host: str = "127.0.0.1") -> bool:
    """Is ``pid`` the process listening on ``port``?

    A bare "is the port listening" check cannot tell a correct restart
    from a stray: this host may already run its own daemon on qmd's
    default port, so 8181 answering says nothing about which process
    answered. Reading the pid out of the socket's inode and comparing it
    to the pidfile's is what ties the two together.
    """
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
    """The inode of the LISTEN socket on ``(host, port)``, from /proc/net/tcp.

    Returns ``None`` when nothing is listening. ``/proc/net/tcp`` stores
    the local address as a little-endian hex ``IP:PORT``, which is why
    this is not done with :mod:`socket`.
    """
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


def test_a_recycle_probe_against_a_dead_port_is_the_measured_shape() -> None:
    """The exception the recycle must retry, straight from fastmcp.

    The unit tests in ``tests/unit/qmd/test_daemon_recycle.py`` construct
    this shape by hand. This one asks fastmcp for it, so the unit tests'
    hand-built exception cannot drift from reality unnoticed — a
    fastmcp upgrade that changed the wrapping would show up here as a
    changed class name, and
    ``test_the_measured_exception_is_not_in_any_legacy_arm`` would then
    deserve re-deriving.
    """
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
