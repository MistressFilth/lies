"""`lies lint --fix --force-repair` propagation."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from lies.cli import app
from lies.lock_errors import WikiFlockUnrepairable, WikiLockBusy
from lies.memory.service import _acquire_wiki_flock
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


def _capture_production_unrepairable_message(monkeypatch: pytest.MonkeyPatch, wiki: Wiki) -> str:
    """Drive ``_acquire_wiki_flock(wiki, force_repair=True)`` to its
    production ``WikiFlockUnrepairable`` raise site and capture the
    message text.

    The flock acquisition normally takes the success path; forcing
    ``acquire_create_lock`` to return ``None`` exercises the
    ``if result is None and force_repair`` branch where the
    operator-actionable message is constructed. We avoid hard-coding
    the message so a reword in ``service.py:100-106`` doesn't leave
    this test green while asserting stale text.
    """
    import lies.memory.service as service_module

    def _stub_acquire(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(service_module, "acquire_create_lock", _stub_acquire)

    try:
        _acquire_wiki_flock(wiki, force_repair=True).__enter__()
    except WikiFlockUnrepairable as exc:
        msg = str(exc)
        assert msg, "production raise site produced an empty message"
        return msg
    raise AssertionError(
        "_acquire_wiki_flock did not raise WikiFlockUnrepairable; "
        "test setup is broken (acquire_create_lock stub not engaged?)"
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
    fake_orch.run_lint.side_effect = WikiLockBusy(
        f"wiki memory lock is held by another process: {fake_wiki.runtime_root / 'memory.lock.create'}"
    )
    monkeypatch.setattr("lies.cli.Orchestrator", lambda *_a, **_kw: fake_orch)

    result = runner.invoke(app, ["lint", "--name", "mywiki", "--fix"])
    assert result.exit_code == 1, _combined(result)
    assert "wiki memory lock" in _combined(result)


def test_lint_fix_force_repair_propagates_unrepairable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Real-path test for the CLI's ``--force-repair`` propagation.

    Drives the production-raised ``WikiFlockUnrepairable`` message
    constructed by ``_acquire_wiki_flock`` (Task 2) through the CLI
    handler. The CLI catches the flock error and prints the
    operator-actionable message to stderr.

    Per the M1 spec's "Risks + mitigations" section, the
    ``WikiFlockUnrepairable`` from ``_acquire_wiki_flock`` deliberately
    omits pid (the file is unlinked before the retry); the
    spec-mandated substring is ``lies flock mywiki force-repair``.
    ``flock_force_repair`` is the code path that *does* surface pid;
    the new flock test in ``tests/unit/cli/test_cli_flock.py`` pins
    that behavior.
    """
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))

    fake_wiki = _fake_wiki("mywiki")

    monkeypatch.setattr("lies.cli.resolve_wiki", lambda _name=None: fake_wiki)
    monkeypatch.setattr("lies.cli.WikiLinkResolver.build", lambda _paths: object())

    # Derive the expected message from the production raise site rather
    # than hand-copying it: a reword in ``service.py:100-106`` would
    # otherwise leave this test green while asserting stale text.
    production_msg = _capture_production_unrepairable_message(monkeypatch, fake_wiki)

    from lies.orchestrator import Orchestrator

    # Bypass ``__init__`` (which loads models); set just the attributes
    # the CLI's lint handler actually touches. The ``Orchestrator.__new__``
    # seam is the same one the existing real-path test below uses.
    orch = Orchestrator.__new__(Orchestrator)
    orch.wiki = fake_wiki
    orch.run_lint = MagicMock(side_effect=WikiFlockUnrepairable(production_msg))

    monkeypatch.setattr("lies.cli.Orchestrator", lambda *_a, **_kw: orch)

    result = runner.invoke(app, ["lint", "--name", "mywiki", "--fix", "--force-repair"])
    assert result.exit_code == 1, _combined(result)
    # Assert the full production message is echoed verbatim to stderr.
    combined = _combined(result)
    assert production_msg in combined


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
    error. The re-raise is exercised end-to-end and the assertions
    below confirm ``WikiFlockUnrepairable`` and ``WikiLockBusy``
    propagate through ``pytest.raises`` (no CLI invocation here; the
    CLI-side assertions live in
    ``test_lint_fix_force_repair_propagates_unrepairable`` above).
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
        "memory flock for wiki 'mywiki' could not be force-reaped; "
        "a live contender won the second attempt. Run `lies flock mywiki "
        "status` to inspect, then `lies flock mywiki force-repair` "
        "or kill the contender manually."
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
