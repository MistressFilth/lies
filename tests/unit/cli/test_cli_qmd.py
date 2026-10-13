"""``lies qmd <cmd>`` must resolve its lazily-bound lifecycle primitives."""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from lies.cli import qmd as qmd_cli


@dataclass
class _Snapshot:
    running: bool = True
    pid: int = 4242
    port: int = 8181
    url: str = "http://127.0.0.1:8181"


def test_status_command_resolves_the_lifecycle_primitive(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A bare-name lookup for a lazily-bound lifecycle helper would raise ``NameError``."""
    monkeypatch.setattr(qmd_cli, "status", lambda port: _Snapshot(port=port))
    qmd_cli.status_cmd(port=9999)
    payload = json.loads(capsys.readouterr().out)
    assert payload["running"] is True
    assert payload["pid"] == 4242
    assert payload["port"] == 9999


@pytest.mark.parametrize(
    ("command", "attr", "kwargs"),
    [
        ("up", "_up", {"port": 8181}),
        ("down", "_down", {"port": 8181}),
        ("recycle_cmd", "recycle", {"port": 8181, "ready_timeout": 0.0}),
    ],
)
def test_every_qmd_command_resolves_its_primitive(
    command: str,
    attr: str,
    kwargs: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(qmd_cli, attr, lambda **kw: _Snapshot())
    getattr(qmd_cli, command)(**kwargs)
    assert capsys.readouterr().out.strip()


def test_a_command_resolves_with_nothing_cached_in_globals(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No lifecycle name is ever bound into ``globals()``."""
    monkeypatch.delitem(qmd_cli.__dict__, "status", raising=False)
    qmd_cli.status_cmd(port=qmd_cli._DEFAULT_PORT)
    assert json.loads(capsys.readouterr().out)["port"] == qmd_cli._DEFAULT_PORT


def test_lifecycle_helper_caches_so_a_later_patch_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A monkeypatch after the first resolution is what later calls see."""
    qmd_cli._lifecycle("status")
    sentinel = object()
    monkeypatch.setattr(qmd_cli, "status", sentinel)
    assert qmd_cli._lifecycle("status") is sentinel
