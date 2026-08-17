"""`lies lint --fix --force-repair` propagation."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from lies.cli import app
from lies.lock_errors import WikiFlockUnrepairable, WikiLockBusy
from lies.wiki.wiki import Wiki

runner = CliRunner()


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in [
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
    ]:
        monkeypatch.delenv(k, raising=False)


def _combined(result) -> str:
    """Click 8.2+ splits stderr from ``.output``; tolerate either layout."""
    return (result.stdout or "") + (result.stderr or "")


def _fake_wiki(name: str = "mywiki") -> Wiki:
    """Build a minimal Wiki so ``resolve_wiki`` returns without hitting disk.

    The lint command touches ``wiki.wiki_dir`` and ``wiki.raw_dir`` only
    inside ``WikiLinkResolver.build``; we mock that too, so the Wiki's
    paths can be ``tmp_path``-rooted without creating real subdirs.
    """
    root = Path("/tmp") / f"fake-wiki-{name}"
    return Wiki(
        name=name,
        data_root=root,
        config_root=root / "config",
        cache_root=root / "cache",
        state_root=root / "state",
        runtime_root=root / "runtime",
    )


def test_lint_fix_default_propagates_lock_busy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No ``--force-repair`` flag → existing ``WikiLockBusy`` message; exit 1.

    Patches ``Orchestrator`` so the test never needs a real model /
    API key; the CLI handler must surface the message to stderr and
    exit non-zero.
    """
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))

    fake_wiki = _fake_wiki("mywiki")

    # Mock resolve_wiki to skip registration; mock WikiLinkResolver.build
    # so the lint command's corpus bootstrap never touches the disk.
    monkeypatch.setattr("lies.cli.resolve_wiki", lambda _name=None: fake_wiki)
    monkeypatch.setattr("lies.cli.WikiLinkResolver.build", lambda _paths: object())

    fake_orch = MagicMock()
    fake_orch.run_lint.side_effect = WikiLockBusy("wiki memory lock is held by another process")
    monkeypatch.setattr("lies.cli.Orchestrator", lambda *_a, **_kw: fake_orch)

    result = runner.invoke(app, ["lint", "--name", "mywiki", "--fix"])
    assert result.exit_code == 1, _combined(result)
    assert "wiki memory lock" in _combined(result)


def test_lint_fix_force_repair_propagates_unrepairable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--force-repair`` + still-busy → ``WikiFlockUnrepairable``, exit 1,
    operator-actionable message including pid + ``lies flock <name> force-repair``.
    """
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))

    fake_wiki = _fake_wiki("mywiki")

    monkeypatch.setattr("lies.cli.resolve_wiki", lambda _name=None: fake_wiki)
    monkeypatch.setattr("lies.cli.WikiLinkResolver.build", lambda _paths: object())

    err = WikiFlockUnrepairable(
        "memory flock for wiki 'mywiki' held by live pid 12345 (started T); "
        "force-repair failed after retry. Run `lies flock mywiki force-repair`."
    )
    fake_orch = MagicMock()
    fake_orch.run_lint.side_effect = err
    monkeypatch.setattr("lies.cli.Orchestrator", lambda *_a, **_kw: fake_orch)

    result = runner.invoke(app, ["lint", "--name", "mywiki", "--fix", "--force-repair"])
    assert result.exit_code == 1, _combined(result)
    combined = _combined(result)
    assert "pid 12345" in combined
    assert "lies flock mywiki force-repair" in combined
