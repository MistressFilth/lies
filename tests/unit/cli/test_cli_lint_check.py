"""`lies lint --check <category>` — the CLI half of the prompt's scoping.

The MCP `lint` tool has taken a `check` argument since 0.42.0 and the
`lint` prompt renders `--check <name>` for it. The CLI had no such
flag, so `lies lint --check orphans` failed where the tool worked —
two surfaces documenting one feature, one of them unable to run it.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from lies.cli import app
from lies.wiki.wiki import Wiki

runner = CliRunner()


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in (
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
    ):
        monkeypatch.delenv(k, raising=False)


def _patch_wiki(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> MagicMock:
    """Point the command at a fake wiki and capture the run_lint kwargs."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    root = tmp_path / "fake-wiki"
    wiki = Wiki(
        name="mywiki",
        data_root=root,
        config_root=root / "config",
        cache_root=root / "cache",
        state_root=root / "state",
        runtime_root=root / "runtime",
    )
    monkeypatch.setattr("lies.cli.resolve_wiki", lambda _name=None: wiki)
    monkeypatch.setattr("lies.cli.WikiLinkResolver.build", lambda _paths: object())
    fake_orch = MagicMock()
    fake_orch.run_lint.return_value = "## report"
    monkeypatch.setattr("lies.cli.Orchestrator", lambda *_a, **_kw: fake_orch)
    return fake_orch


def test_check_reaches_run_lint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_orch = _patch_wiki(monkeypatch, tmp_path)
    result = runner.invoke(app, ["lint", "--name", "mywiki", "--check", "orphans"])
    assert result.exit_code == 0, result.output
    assert fake_orch.run_lint.call_args.kwargs["check"] == "orphans"


def test_no_check_passes_none(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_orch = _patch_wiki(monkeypatch, tmp_path)
    result = runner.invoke(app, ["lint", "--name", "mywiki"])
    assert result.exit_code == 0, result.output
    assert fake_orch.run_lint.call_args.kwargs["check"] is None


def test_check_composes_with_fix(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``--check`` narrows the repair agent's input, so the two compose."""
    fake_orch = _patch_wiki(monkeypatch, tmp_path)
    result = runner.invoke(app, ["lint", "--name", "mywiki", "--check", "orphan", "--fix"])
    assert result.exit_code == 0, result.output
    kwargs = fake_orch.run_lint.call_args.kwargs
    assert kwargs["check"] == "orphan"
    assert kwargs["apply"] is True
