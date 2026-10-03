"""Tests for the ``lies qmd`` operator sub-app.

Mirrors ask's ``scripts/qmd-daemon.py``: ``status``, ``up``,
``down``, ``recycle``. All tests mock the lifecycle primitives
at the ``lies.cli.qmd`` module boundary — the CLI binds them at
import time, so patching ``lies.qmd.lifecycle.status`` would not
intercept the CLI's call. The CLI does no subprocess work itself
and mocks keep tests deterministic across hosts (the developer
machine runs a real daemon on 8181).
"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from lies.cli import qmd as qmd_cli
from lies.cli.qmd import app
from lies.qmd.lifecycle import DaemonStatus


def test_qmd_status_prints_json(monkeypatch) -> None:
    """``lies qmd status`` prints a JSON object containing the daemon snapshot.

    Lifecycle ``status()`` is mocked so the test is independent of
    the host's real daemon on 8181.
    """
    fake = DaemonStatus(running=True, pid=4242, port=8181, url="http://127.0.0.1:8181/mcp")
    monkeypatch.setattr(qmd_cli, "status", lambda port=8181: fake)

    runner = CliRunner()
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert '"running"' in result.stdout


def test_qmd_status_serializes_all_daemon_status_fields(monkeypatch) -> None:
    """The JSON payload includes every field ``DaemonStatus`` exposes.

    Pins ``running``, ``pid``, ``port``, and ``url`` so downstream
    shell callers parsing the JSON can rely on the schema. The
    ``index`` block is null under the autouse XDG isolation
    (``qmd_index_path()`` resolves to ``tmp_path/xdg/cache/qmd/index.sqlite``
    which is never created here).
    """
    fake = DaemonStatus(running=False, pid=None, port=8181, url="http://127.0.0.1:8181/mcp")
    monkeypatch.setattr(qmd_cli, "status", lambda port=8181: fake)

    runner = CliRunner()
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload == {
        "running": False,
        "pid": None,
        "port": 8181,
        "url": "http://127.0.0.1:8181/mcp",
        "index": None,
    }


def test_qmd_status_honors_port_flag(monkeypatch) -> None:
    """``--port`` is forwarded to the lifecycle ``status()`` call."""
    captured: dict[str, int] = {}

    def fake_status(port: int = 8181) -> DaemonStatus:
        captured["port"] = port
        return DaemonStatus(running=False, pid=None, port=port, url=f"http://127.0.0.1:{port}/mcp")

    monkeypatch.setattr(qmd_cli, "status", fake_status)

    runner = CliRunner()
    result = runner.invoke(app, ["status", "--port", "9000"])
    assert result.exit_code == 0, result.output
    assert captured == {"port": 9000}
    assert json.loads(result.stdout)["port"] == 9000


def test_qmd_up_invokes_up_and_prints_pid_and_url(monkeypatch) -> None:
    """``lies qmd up`` calls ``_up`` and announces the daemon's pid + url."""
    expected = DaemonStatus(running=True, pid=7777, port=8181, url="http://127.0.0.1:8181/mcp")
    monkeypatch.setattr(qmd_cli, "_up", lambda port=8181: expected)

    runner = CliRunner()
    result = runner.invoke(app, ["up"])
    assert result.exit_code == 0, result.output
    assert "7777" in result.stdout
    assert "http://127.0.0.1:8181/mcp" in result.stdout


def test_qmd_up_forwards_port_flag(monkeypatch) -> None:
    """``--port`` is forwarded to ``_up``."""
    captured: dict[str, int] = {}

    def fake_up(port: int = 8181) -> DaemonStatus:
        captured["port"] = port
        return DaemonStatus(running=True, pid=1, port=port, url="")

    monkeypatch.setattr(qmd_cli, "_up", fake_up)

    runner = CliRunner()
    result = runner.invoke(app, ["up", "--port", "8181"])
    assert result.exit_code == 0, result.output
    assert captured == {"port": 8181}


def test_qmd_down_invokes_down(monkeypatch) -> None:
    """``lies qmd down`` calls ``_down`` and prints a stop confirmation.

    The lifecycle ``_down`` is best-effort and returns ``None``;
    the CLI prints the operator-facing confirmation.
    """
    captured: dict[str, int] = {}

    def fake_down(port: int = 8181) -> None:
        captured["port"] = port

    monkeypatch.setattr(qmd_cli, "_down", fake_down)

    runner = CliRunner()
    result = runner.invoke(app, ["down"])
    assert result.exit_code == 0, result.output
    assert captured == {"port": 8181}
    assert "stopped" in result.stdout.lower()


def test_qmd_down_forwards_port_flag(monkeypatch) -> None:
    """``--port`` is forwarded to ``_down``."""
    captured: dict[str, int] = {}

    def fake_down(port: int = 8181) -> None:
        captured["port"] = port

    monkeypatch.setattr(qmd_cli, "_down", fake_down)

    runner = CliRunner()
    result = runner.invoke(app, ["down", "--port", "9000"])
    assert result.exit_code == 0, result.output
    assert captured == {"port": 9000}


def test_qmd_recycle_invokes_recycle_and_prints_status(monkeypatch) -> None:
    """``lies qmd recycle`` calls ``recycle`` and announces the fresh daemon."""
    expected = DaemonStatus(running=True, pid=8888, port=8181, url="http://127.0.0.1:8181/mcp")
    monkeypatch.setattr(qmd_cli, "recycle", lambda port=8181, ready_timeout=30.0: expected)

    runner = CliRunner()
    result = runner.invoke(app, ["recycle"])
    assert result.exit_code == 0, result.output
    assert "8888" in result.stdout
    assert "http://127.0.0.1:8181/mcp" in result.stdout


def test_qmd_recycle_forwards_port_and_ready_timeout(monkeypatch) -> None:
    """Both ``--port`` and ``--ready-timeout`` are forwarded to ``recycle``."""
    captured: dict[str, object] = {}

    def fake_recycle(port: int = 8181, ready_timeout: float = 30.0) -> DaemonStatus:
        captured["port"] = port
        captured["ready_timeout"] = ready_timeout
        return DaemonStatus(running=True, pid=1, port=port, url="")

    monkeypatch.setattr(qmd_cli, "recycle", fake_recycle)

    runner = CliRunner()
    result = runner.invoke(app, ["recycle", "--port", "9000", "--ready-timeout", "12.5"])
    assert result.exit_code == 0, result.output
    assert captured == {"port": 9000, "ready_timeout": 12.5}
