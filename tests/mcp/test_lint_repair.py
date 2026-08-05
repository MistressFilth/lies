"""MCP tests for lint(fix=...) wiring."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

import pytest

from lies.agents.linter import LintReport
from lies.agents.repair_models import CreateStub, RepairPlan
from lies.mcp.server import lint
from lies.orchestrator import Orchestrator
from lies.wiki.wiki import Wiki
from tests.conftest import make_wiki


@pytest.fixture(autouse=True)
def mock_lies_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default ``LIES_MODEL`` to ``"test"`` so ``Orchestrator`` can build."""
    monkeypatch.setenv("LIES_MODEL", "test")


WIKI_NAME = "lint-mcp"


@pytest.fixture
def wiki(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Wiki:
    """Register a wiki under ``WIKI_NAME`` and return its ``Wiki`` handle.

    The MCP ``lint`` tool resolves wikis by name through ``resolve_wiki``,
    which in turn requires the XDG-rooted data directory to exist. So we
    build the wiki at ``XDG_DATA_HOME/lies/<WIKI_NAME>`` and set
    ``LIES_WIKI_NAME`` so the tool finds it.
    """
    data_root = Wiki.data_root_for(WIKI_NAME)
    data_root.mkdir(parents=True, exist_ok=True)
    for sub in ("wiki", "raw"):
        (data_root / sub).mkdir(parents=True, exist_ok=True)
    (data_root / "wiki" / "index.md").write_text("# Index\n", encoding="utf-8")
    subprocess.run(["git", "init", "--initial-branch=main", str(data_root)], check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=data_root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=data_root, check=True)
    subprocess.run(["git", "add", "."], cwd=data_root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=data_root, check=True)
    monkeypatch.setenv("LIES_WIKI_NAME", WIKI_NAME)
    return make_wiki(name=WIKI_NAME, data_root=data_root)


def _no_agent_run_sync() -> mock._patch:
    """Patch ``Orchestrator._agent.run_sync`` so the TestModel loop is skipped.

    Patches at the class level so the fresh ``Orchestrator`` constructed
    inside the MCP tool sees the same mock. ``Orchestrator._run_repair_agent``
    is patched separately in each test, so the repair agent's own
    ``run_sync`` is never reached.
    """
    throwaway = Orchestrator(
        wiki=make_wiki(name="throwaway", data_root=Path.cwd()),
        model="test",
    )
    return mock.patch.object(
        type(throwaway._agent), "run_sync", return_value=mock.Mock(output="lint done")
    )


def test_lint_default_does_not_apply(wiki: Wiki) -> None:
    with (
        _no_agent_run_sync(),
        mock.patch.object(
            Orchestrator,
            "_call_linter",
            return_value=(LintReport(findings=[], report_markdown=""), None),
        ),
        mock.patch.object(Orchestrator, "_run_repair_agent") as mock_repair,
    ):
        report = lint(name=WIKI_NAME)
    assert isinstance(report, str)
    mock_repair.assert_not_called()


def test_lint_fix_true_invokes_repair(wiki: Wiki) -> None:
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
        mock.patch.object(
            Orchestrator,
            "_call_linter",
            return_value=(LintReport(findings=[], report_markdown=""), None),
        ),
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
        report = lint(name=WIKI_NAME, fix=True)
    assert isinstance(report, str)


def test_lint_fix_false_matches_default(wiki: Wiki) -> None:
    with (
        _no_agent_run_sync(),
        mock.patch.object(
            Orchestrator,
            "_call_linter",
            return_value=(LintReport(findings=[], report_markdown=""), None),
        ),
        mock.patch.object(Orchestrator, "_run_repair_agent") as mock_repair,
    ):
        report = lint(name=WIKI_NAME, fix=False)
    mock_repair.assert_not_called()
    assert isinstance(report, str)
