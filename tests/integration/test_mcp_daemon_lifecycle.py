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

from lies.cli import app
from lies.mcp import daemon
from lies.wiki.layout import WikiLayout

pytestmark = pytest.mark.integration

runner = CliRunner()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def wiki(tmp_path: Path) -> Path:
    WikiLayout(tmp_path).init()
    return tmp_path


@pytest.fixture
def port() -> int:
    return _free_port()


@pytest.fixture(autouse=True)
def _always_stop(wiki: Path):
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
    with urllib.request.urlopen(request, timeout=10) as response:
        return {"status": response.status, "body": response.read().decode("utf-8")}


def test_full_lifecycle(wiki: Path, port: int) -> None:
    up_result = runner.invoke(
        app, ["mcp", "up", "--wiki-root", str(wiki), "--port", str(port), "--timeout", "30"]
    )
    assert up_result.exit_code == 0, up_result.output
    assert str(port) in up_result.stdout

    record = daemon.read_record(wiki)
    assert record is not None
    assert record.port == port
    assert record.transport == "http"
    assert daemon.process_alive(record.pid) is True

    handshake = _initialize(daemon.daemon_url(record))
    assert handshake["status"] == 200
    assert "lies" in handshake["body"]

    status_result = runner.invoke(app, ["mcp", "status", "--wiki-root", str(wiki)])
    assert status_result.exit_code == 0
    assert "running" in status_result.stdout
    assert str(record.pid) in status_result.stdout

    second_up = runner.invoke(app, ["mcp", "up", "--wiki-root", str(wiki), "--port", str(port)])
    assert second_up.exit_code == 0
    assert "already running" in second_up.stdout
    assert daemon.read_record(wiki) is not None
    assert daemon.read_record(wiki).pid == record.pid  # type: ignore[union-attr]

    down_result = runner.invoke(app, ["mcp", "down", "--wiki-root", str(wiki)])
    assert down_result.exit_code == 0
    assert str(record.pid) in down_result.stdout
    assert daemon.read_record(wiki) is None

    stopped = runner.invoke(app, ["mcp", "status", "--wiki-root", str(wiki)])
    assert stopped.exit_code == 1
    assert "stopped" in stopped.stdout

    second_down = runner.invoke(app, ["mcp", "down", "--wiki-root", str(wiki)])
    assert second_down.exit_code == 0
    assert "no daemon running" in second_down.stdout


def test_down_leaves_the_qmd_daemon_running(wiki: Path, port: int) -> None:
    """qmd is machine-global and shared — `down` must never take it with it.

    This is the one behavior that can damage state outside the wiki, so it
    is asserted against the real qmd rather than a mock.
    """
    from lies.qmd.daemon import qmd_daemon_state

    before = qmd_daemon_state()
    if not before.installed:
        pytest.skip("qmd is not installed")

    up_result = runner.invoke(
        app, ["mcp", "up", "--wiki-root", str(wiki), "--port", str(port), "--timeout", "30"]
    )
    assert up_result.exit_code == 0, up_result.output

    started = qmd_daemon_state()
    assert started.running is True

    down_result = runner.invoke(app, ["mcp", "down", "--wiki-root", str(wiki)])
    assert down_result.exit_code == 0

    after = qmd_daemon_state()
    assert after.running is True
    assert after.pid == started.pid


def test_up_fails_cleanly_when_port_is_taken(wiki: Path, port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
        held.bind(("127.0.0.1", port))
        held.listen(1)
        result = runner.invoke(app, ["mcp", "up", "--wiki-root", str(wiki), "--port", str(port)])
    assert result.exit_code == 1
    assert daemon.read_record(wiki) is None


def test_daemon_artifacts_are_gitignored(wiki: Path, port: int) -> None:
    result = runner.invoke(
        app, ["mcp", "up", "--wiki-root", str(wiki), "--port", str(port), "--timeout", "30"]
    )
    assert result.exit_code == 0, result.output
    body = (wiki / ".gitignore").read_text(encoding="utf-8")
    assert ".lies/mcp.pid" in body
    assert ".lies/mcp.pid.create" in body
    assert ".lies/mcp.log" in body
