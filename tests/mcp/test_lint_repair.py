"""MCP tests for lint(fix=...) wiring."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

import pytest

from lies.agents.repair_models import CreateStub, RepairPlan
from lies.mcp.server import lint
from lies.orchestrator import Orchestrator
from lies.wiki.layout import WikiLayout


@pytest.fixture(autouse=True)
def mock_lies_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default ``LIES_MODEL`` to ``"test"`` so ``Orchestrator`` can build."""
    monkeypatch.setenv("LIES_MODEL", "test")


@pytest.fixture
def wiki(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> WikiLayout:
    root = tmp_path / "wiki"
    for sub in ("wiki", ".lies", "raw"):
        (root / sub).mkdir(parents=True)
    (root / "wiki" / "index.md").write_text("# Index\n", encoding="utf-8")
    subprocess.run(["git", "init", "--initial-branch=main", str(root)], check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True)
    monkeypatch.setenv("LIES_WIKI_ROOT", str(root))
    return WikiLayout(root)


def _no_agent_run_sync() -> mock._patch:
    """Patch ``Orchestrator._agent.run_sync`` so the TestModel loop is skipped.

    Patches at the class level so the fresh ``Orchestrator`` constructed
    inside the MCP tool sees the same mock. ``Orchestrator._run_repair_agent``
    is patched separately in each test, so the repair agent's own
    ``run_sync`` is never reached.
    """
    throwaway = Orchestrator(wiki_root=Path.cwd(), model="test")
    return mock.patch.object(
        type(throwaway._agent), "run_sync", return_value=mock.Mock(output="lint done")
    )


def test_lint_default_does_not_apply(wiki: WikiLayout) -> None:
    with _no_agent_run_sync(), mock.patch.object(Orchestrator, "_run_repair_agent") as mock_repair:
        report = lint(wiki_root=str(wiki.root))
    assert isinstance(report, str)
    mock_repair.assert_not_called()


def test_lint_fix_true_invokes_repair(wiki: WikiLayout) -> None:
    plan = RepairPlan(
        operations=[
            CreateStub(
                path="concepts/x.md",
                title="X",
                finding_index=0,
                pages=[],
                rationale="new",
                evidence=["f0"],
            ),
        ],
        rationale="r",
        evidence=["f0"],
    )
    with (
        _no_agent_run_sync(),
        mock.patch.object(Orchestrator, "_run_repair_agent", return_value=plan),
        mock.patch.object(
            Orchestrator,
            "_apply_repair_plan",
            return_value=mock.MagicMock(
                applied=[],
                skipped=[],
                deferred=[],
                errors=[],
                applied_paths=[],
            ),
        ),
    ):
        report = lint(wiki_root=str(wiki.root), fix=True)
    assert isinstance(report, str)


def test_lint_fix_false_matches_default(wiki: WikiLayout) -> None:
    with _no_agent_run_sync(), mock.patch.object(Orchestrator, "_run_repair_agent") as mock_repair:
        report = lint(wiki_root=str(wiki.root), fix=False)
    mock_repair.assert_not_called()
    assert isinstance(report, str)
