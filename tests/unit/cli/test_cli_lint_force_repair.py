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


def test_lint_fix_force_repair_unrepairable_reraised_through_apply(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``WikiFlockUnrepairable`` from the service reaches the CLI handlers.

    The MagicMock-based tests above bypass the orchestrator's broad
    ``except Exception`` in ``_apply_repair_plan``. After the fix,
    that handler re-raises ``WikiFlockUnrepairable`` / ``WikiLockBusy``
    before the broad except, so they reach the CLI's top-level
    ``typer.Exit(code=1)`` handlers. This test drives the real
    ``Orchestrator._apply_repair_plan`` path: a real ``Orchestrator``
    instance built via ``__new__`` (so we skip ``__init__``'s model
    loading) with a mocked memory service that raises the flock
    error. The re-raise is exercised end-to-end and the CLI exits
    non-zero with the operator-actionable message — which includes
    the brief's verbatim substrings (``pid 12345`` and
    ``lies flock mywiki force-repair``).
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

    from lies.agents.repair_models import AppendLink, RepairPlan
    from lies.agents.repair_validation import ValidatedRepairPlan
    from lies.orchestrator import Orchestrator

    # Bypass __init__ (which loads models); set just the attributes
    # ``_apply_repair_plan`` actually touches.
    orch = Orchestrator.__new__(Orchestrator)
    orch.wiki = fake_wiki
    orch._memory_service = MagicMock()
    orch._memory_service.apply_repair_plan.side_effect = err

    # Build a minimal non-noop ValidatedRepairPlan so the method
    # actually reaches ``self._memory_service.apply_repair_plan``.
    op = AppendLink(
        finding_index=0,
        pages=[],
        rationale="test",
        evidence=["src1"],
        target_path="concepts/foo.md",
        link_text="foo",
        append_to="concepts/bar.md",
    )
    plan = RepairPlan(operations=[op], rationale="test", evidence=["src1"])
    validated = ValidatedRepairPlan(plan=plan, dropped_ops=())

    # The real method must re-raise — the broad ``except Exception``
    # in the pre-fix code would have swallowed it.
    with pytest.raises(WikiFlockUnrepairable):
        orch._apply_repair_plan(validated, force_repair=True)

    # And it must NOT be captured into a RepairReceipt (sanity check
    # on the same code path).
    orch._memory_service.apply_repair_plan.side_effect = WikiLockBusy(
        "wiki memory lock is held by another process"
    )
    with pytest.raises(WikiLockBusy):
        orch._apply_repair_plan(validated, force_repair=False)
