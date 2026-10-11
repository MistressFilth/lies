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
