"""Real up → handshake → status → down cycle against a live daemon.

Every other daemon test mocks the process boundary. This one spawns a
real child, so it is the only place a broken re-exec path, a wrong
transport name, or a wrong mount path can be caught.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lies import xdg
from lies.cli import app
from lies.mcp import daemon
from lies.wiki.layout import WikiLayout
from lies.wiki.wiki import Wiki

pytestmark = pytest.mark.integration

runner = CliRunner()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def wiki(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Wiki:
    """Build a Wiki whose XDG roots all live under ``tmp_path``.

    The daemon's pidfile and log are now under
    ``$XDG_RUNTIME_DIR/lies/<wiki>`` and ``$XDG_STATE_HOME/lies/<wiki>``
    respectively. Pointing all five role roots at ``tmp_path`` keeps the
    test self-contained: nothing leaks outside the fixture's sandbox.
    """
    monkeypatch.setenv("LIES_XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("LIES_XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("LIES_XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("LIES_XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("LIES_XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    name = "test"
    data_root = Wiki.data_root_for(name)
    wiki = Wiki(
        name=name,
        data_root=data_root,
        config_root=xdg.config_home() / "lies" / name,
        cache_root=xdg.cache_home() / "lies" / name,
        state_root=xdg.state_home() / "lies" / name,
        runtime_root=xdg.runtime_dir_for(name),
    )
    WikiLayout(wiki.data_root).init()
    return wiki


@pytest.fixture
def port() -> int:
    return _free_port()


@pytest.fixture(autouse=True)
def _always_stop(wiki: Wiki):
    yield
    try:
        daemon.stop_daemon(wiki, grace=3.0)
    except daemon.DaemonError:
        pass


def _initialize(url: str) -> dict:
    """Send an MCP `initialize` request and return the decoded response."""
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "lies-test", "version": "0"},
            },
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return {"status": response.status, "body": response.read().decode("utf-8")}
    except urllib.error.HTTPError as exc:
        # A non-2xx is still a response. Return it so the status
        # assertion reports the real code instead of a raised traceback.
        return {"status": exc.code, "body": exc.read().decode("utf-8", errors="replace")}
    except urllib.error.URLError as exc:
        raise AssertionError(f"nothing answered at {url}: {exc.reason}") from exc


def _decode_jsonrpc(body: str) -> dict:
    """Decode the JSON-RPC message out of a bare-JSON or SSE response body.

    FastMCP's streamable-http transport answers with `text/event-stream`
    when the client accepts it, so the payload arrives framed as
    ``data: {...}`` rather than as bare JSON. Both shapes carry the same
    message; this returns it either way.
    """
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        pass
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped.startswith("data:"):
            continue
        try:
            return json.loads(stripped[len("data:") :].strip())
        except json.JSONDecodeError:
            continue
    raise AssertionError(f"no JSON-RPC payload in response body: {body!r}")


def test_full_lifecycle(wiki: Wiki, port: int) -> None:
    up_result = runner.invoke(
        app,
        ["mcp", "up", "--name", wiki.name, "--port", str(port), "--timeout", "30"],
    )
    assert up_result.exit_code == 0, up_result.output
    assert str(port) in up_result.stdout

    record = daemon.read_record(wiki)
    assert record is not None
    assert record.port == port
    assert record.transport == "http"
    assert daemon.process_alive(record.pid) is True

    handshake = _initialize(daemon.daemon_url(record))
    assert handshake["status"] == 200, handshake["body"]
    message = _decode_jsonrpc(handshake["body"])
    assert "error" not in message, f"initialize returned an error: {message.get('error')}"
    assert "result" in message, f"initialize returned no result: {message}"
    server_info = message["result"].get("serverInfo", {})
    assert server_info.get("name") == "lies", (
        f"handshake did not come from the lies server: {server_info}"
    )

    status_result = runner.invoke(app, ["mcp", "status", "--name", wiki.name])
    assert status_result.exit_code == 0
    assert "running" in status_result.stdout
    assert str(record.pid) in status_result.stdout

    second_up = runner.invoke(app, ["mcp", "up", "--name", wiki.name, "--port", str(port)])
    assert second_up.exit_code == 0
    assert "already running" in second_up.stdout
    assert daemon.read_record(wiki) is not None
    assert daemon.read_record(wiki).pid == record.pid  # type: ignore[union-attr]

    down_result = runner.invoke(app, ["mcp", "down", "--name", wiki.name])
    assert down_result.exit_code == 0
    assert str(record.pid) in down_result.stdout
    assert daemon.read_record(wiki) is None

    stopped = runner.invoke(app, ["mcp", "status", "--name", wiki.name])
    assert stopped.exit_code == 1
    assert "stopped" in stopped.stdout

    second_down = runner.invoke(app, ["mcp", "down", "--name", wiki.name])
    assert second_down.exit_code == 0
    assert "no daemon running" in second_down.stdout


def test_down_leaves_the_qmd_daemon_running(wiki: Wiki, port: int) -> None:
    """qmd is machine-global and shared — `down` must never take it with it.

    This is the one behavior that can damage state outside the wiki, so it
    is asserted against the real qmd rather than a mock.
    """
    from lies.qmd.daemon import qmd_daemon_state

    before = qmd_daemon_state()
    if not before.installed:
        pytest.skip("qmd is not installed; there is no shared daemon to protect")
    if not before.running or before.pid is None:
        pytest.skip(
            "qmd is installed but not running; with no pid captured up front, "
            "comparing daemon identity across up/down would prove nothing"
        )

    up_result = runner.invoke(
        app,
        ["mcp", "up", "--name", wiki.name, "--port", str(port), "--timeout", "30"],
    )
    assert up_result.exit_code == 0, up_result.output

    started = qmd_daemon_state()
    assert started.running is True
    assert started.pid == before.pid, (
        f"up recycled the shared qmd daemon instead of reusing it: {before.pid} -> {started.pid}"
    )

    down_result = runner.invoke(app, ["mcp", "down", "--name", wiki.name])
    assert down_result.exit_code == 0

    after = qmd_daemon_state()
    assert after.running is True
    assert after.pid == before.pid, (
        f"down disturbed the shared qmd daemon: {before.pid} -> {after.pid}"
    )


def test_up_fails_cleanly_when_port_is_taken(wiki: Wiki, port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
        held.bind(("127.0.0.1", port))
        held.listen(1)
        result = runner.invoke(app, ["mcp", "up", "--name", wiki.name, "--port", str(port)])
    assert result.exit_code == 1
    assert daemon.read_record(wiki) is None
