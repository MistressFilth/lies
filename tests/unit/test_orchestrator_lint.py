"""Unit tests for the orchestrator's lint apply path."""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

import pytest

from lies.agents.repair_models import (
    CreateStub,
    RepairPlan,
    RepairReceipt,
)
from lies.memory.models import (
    OperationKind,
    PageReference,
)
from lies.orchestrator import Orchestrator


@pytest.fixture
def orch(tmp_path: Path) -> Orchestrator:
    root = tmp_path / "wiki"
    for sub in ("wiki", ".lies", "raw"):
        (root / sub).mkdir(parents=True)
    (root / "wiki" / "index.md").write_text("# Index\n", encoding="utf-8")
    (root / ".lies" / "schema.md").write_text("## Page types\n- concept\n", encoding="utf-8")
    subprocess.run(["git", "init", "--initial-branch=main", str(root)], check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True)
    return Orchestrator(wiki_root=root, model="test")


def _noop_agent_run_sync(self, prompt: str):  # type: ignore[no-untyped-def]
    return mock.Mock(output="lint done")


def test_run_lint_default_does_not_invoke_repair_agent(orch: Orchestrator) -> None:
    with mock.patch.object(type(orch._agent), "run_sync", new=_noop_agent_run_sync), \
         mock.patch.object(orch, "_run_repair_agent") as mock_repair:
        report = orch.run_lint()
    assert isinstance(report, str)
    mock_repair.assert_not_called()


def test_run_lint_apply_invokes_repair_agent(orch: Orchestrator) -> None:
    fake_plan = RepairPlan(
        operations=[
            CreateStub(path="concepts/x.md", title="X", finding_index=0, pages=[], rationale="new", evidence=["f0"]),
        ],
        rationale="r",
        evidence=["f0"],
    )
    fake_receipt = RepairReceipt(
        applied=[
            PageReference(path="concepts/x.md", collection_id="wiki", op=OperationKind.CREATE)
        ],
        skipped=[],
        deferred=[],
        errors=[],
    )
    with mock.patch.object(type(orch._agent), "run_sync", new=_noop_agent_run_sync), \
         mock.patch.object(orch, "_run_repair_agent", return_value=fake_plan) as mock_repair, \
         mock.patch.object(orch, "_apply_repair_plan", return_value=fake_receipt) as mock_apply:
        report = orch.run_lint(apply=True)
    mock_repair.assert_called_once()
    mock_apply.assert_called_once_with(fake_plan)
    assert "applied" in report.lower() or "Applied" in report


def test_run_lint_apply_records_skipped_findings(orch: Orchestrator) -> None:
    fake_plan = RepairPlan(operations=[], rationale="noop", evidence=["f0"])
    fake_receipt = RepairReceipt(
        applied=[],
        skipped=["finding_0: contradiction"],
        deferred=[],
        errors=[],
    )
    with mock.patch.object(type(orch._agent), "run_sync", new=_noop_agent_run_sync), \
         mock.patch.object(orch, "_run_repair_agent", return_value=fake_plan), \
         mock.patch.object(orch, "_apply_repair_plan", return_value=fake_receipt):
        report = orch.run_lint(apply=True)
    assert "skipped" in report.lower() or "Skipped" in report


def test_run_lint_apply_surfaces_errors(orch: Orchestrator) -> None:
    fake_plan = RepairPlan(operations=[], rationale="noop", evidence=["f0"])
    fake_receipt = RepairReceipt(
        applied=[],
        skipped=[],
        deferred=[],
        errors=["repair_agent_failed"],
    )
    with mock.patch.object(type(orch._agent), "run_sync", new=_noop_agent_run_sync), \
         mock.patch.object(orch, "_run_repair_agent", return_value=fake_plan), \
         mock.patch.object(orch, "_apply_repair_plan", return_value=fake_receipt):
        report = orch.run_lint(apply=True)
    assert "errors" in report.lower() or "Errors" in report