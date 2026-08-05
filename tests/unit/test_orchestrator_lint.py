"""Unit tests for the orchestrator's lint apply path."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

import pytest

from lies.agents.linter import LintReport
from lies.agents.repair import RepairAgentDeps
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
from tests.conftest import make_wiki


@pytest.fixture
def orch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Orchestrator:
    root = tmp_path / "wiki"
    for sub in ("wiki", "raw"):
        (root / sub).mkdir(parents=True)
    (root / "wiki" / "index.md").write_text("# Index\n", encoding="utf-8")
    wiki = make_wiki(name="lint", data_root=root)
    wiki.config_root.mkdir(parents=True, exist_ok=True)
    (wiki.config_root / "schema.md").write_text(
        "## Page types\n- concept\n", encoding="utf-8"
    )
    subprocess.run(["git", "init", "--initial-branch=main", str(root)], check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True)
    return Orchestrator(wiki=wiki, model="test")


def _noop_agent_run_sync(self, prompt: str):  # type: ignore[no-untyped-def]
    return mock.Mock(output="lint done")


def test_run_lint_default_does_not_invoke_repair_agent(orch: Orchestrator) -> None:
    with (
        mock.patch.object(type(orch._agent), "run_sync", new=_noop_agent_run_sync),
        mock.patch.object(
            orch, "_call_linter", return_value=(LintReport(findings=[], report_markdown=""), None)
        ),
        mock.patch.object(orch, "_run_repair_agent") as mock_repair,
    ):
        report = orch.run_lint()
    assert isinstance(report, str)
    mock_repair.assert_not_called()


def test_run_lint_apply_invokes_repair_agent(orch: Orchestrator) -> None:
    from lies.agents.linter import LintFinding, LintSeverity
    from lies.agents.repair_validation import ValidatedRepairPlan

    fake_finding = LintFinding(
        severity=LintSeverity.LOW,
        category="missing_page",
        message="missing x",
        pages=["concepts/x.md"],
        safe_to_fix=True,
    )
    fake_plan = RepairPlan(
        operations=[
            CreateStub(
                path="concepts/x.md",
                title="X",
                finding_index=0,
                pages=["concepts/x.md"],
                rationale="new",
                evidence=["f0"],
            ),
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
    with (
        mock.patch.object(type(orch._agent), "run_sync", new=_noop_agent_run_sync),
        mock.patch.object(
            orch,
            "_call_linter",
            return_value=(LintReport(findings=[fake_finding], report_markdown=""), None),
        ),
        mock.patch.object(orch, "_run_repair_agent", return_value=fake_plan) as mock_repair,
        mock.patch.object(orch, "_apply_repair_plan", return_value=fake_receipt) as mock_apply,
    ):
        report = orch.run_lint(apply=True)
    mock_repair.assert_called_once()
    assert mock_apply.call_count == 1
    call_arg = mock_apply.call_args[0][0]
    assert isinstance(call_arg, ValidatedRepairPlan)
    assert call_arg.plan is fake_plan
    assert "applied" in report.lower() or "Applied" in report


def test_run_lint_apply_records_skipped_findings(orch: Orchestrator) -> None:
    fake_plan = RepairPlan(operations=[], rationale="noop", evidence=["f0"])
    fake_receipt = RepairReceipt(
        applied=[],
        skipped=["finding_0: contradiction"],
        deferred=[],
        errors=[],
    )
    with (
        mock.patch.object(type(orch._agent), "run_sync", new=_noop_agent_run_sync),
        mock.patch.object(
            orch, "_call_linter", return_value=(LintReport(findings=[], report_markdown=""), None)
        ),
        mock.patch.object(orch, "_run_repair_agent", return_value=fake_plan),
        mock.patch.object(orch, "_apply_repair_plan", return_value=fake_receipt),
    ):
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
    with (
        mock.patch.object(type(orch._agent), "run_sync", new=_noop_agent_run_sync),
        mock.patch.object(
            orch, "_call_linter", return_value=(LintReport(findings=[], report_markdown=""), None)
        ),
        mock.patch.object(orch, "_run_repair_agent", return_value=fake_plan),
        mock.patch.object(orch, "_apply_repair_plan", return_value=fake_receipt),
    ):
        report = orch.run_lint(apply=True)
    assert "errors" in report.lower() or "Errors" in report


def test_build_lint_report_orphans_are_safe_to_fix(orch: Orchestrator) -> None:
    """The deterministic host-side shell must mark orphan findings
    safe_to_fix=True so the repair agent's HARD RULE permits the
    corresponding UpdateIndex op."""
    from lies.orchestrator import _build_lint_report

    # Seed an orphan page (no inbound links).
    orphan = orch.wiki.wiki_dir / "concepts" / "orphan.md"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text(
        "---\ntitle: Orphan\ntype: concept\n---\n# Orphan\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=orch.wiki.data_root, check=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=orch.wiki.data_root, check=True)

    report = _build_lint_report(orch.wiki)
    orphans = [f for f in report.findings if f.category == "orphan"]
    assert orphans, "expected at least one orphan finding"
    for finding in orphans:
        assert finding.safe_to_fix is True, (
            "orphan findings must be safe_to_fix=True so the repair agent "
            "is permitted to emit an UpdateIndex op"
        )


def test_run_lint_apply_passes_findings_to_repair_agent(orch: Orchestrator) -> None:
    """The LintReport's safe_to_fix flags flow through to the repair agent
    via RepairAgentDeps. Orphans are safe_to_fix=True; any other finding
    (if added by the deterministic shell in future) is False by default."""
    from lies.orchestrator import _build_lint_report

    # Seed an orphan so the deterministic shell produces a finding.
    orphan = orch.wiki.wiki_dir / "concepts" / "orphan.md"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text(
        "---\ntitle: Orphan\ntype: concept\n---\n# Orphan\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=orch.wiki.data_root, check=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=orch.wiki.data_root, check=True)

    # Sanity check: the deterministic shell produces a safe orphan finding.
    pre = _build_lint_report(orch.wiki)
    assert any(f.category == "orphan" and f.safe_to_fix is True for f in pre.findings)

    # Capture the RepairAgentDeps the repair agent receives.
    captured: dict[str, object] = {}

    def fake_repair_agent_run_sync(prompt: str, deps: object = None):  # type: ignore[no-untyped-def]
        captured["deps"] = deps
        return mock.Mock(output=RepairPlan(operations=[], rationale="r", evidence=["f0"]))

    with (
        mock.patch.object(type(orch._agent), "run_sync", new=_noop_agent_run_sync),
        mock.patch.object(
            orch, "_call_linter", return_value=(LintReport(findings=[], report_markdown=""), None)
        ),
        mock.patch.object(orch._repair_agent, "run_sync", new=fake_repair_agent_run_sync),
        mock.patch.object(
            orch,
            "_apply_repair_plan",
            return_value=RepairReceipt(applied=[], skipped=[], deferred=[], errors=[]),
        ),
    ):
        orch.run_lint(apply=True)

    deps = captured.get("deps")
    assert isinstance(deps, RepairAgentDeps), (
        f"repair agent must receive RepairAgentDeps, got {type(deps)!r}"
    )
    received_report: LintReport = deps.lint_report
    assert received_report.findings, "repair agent must receive a non-empty LintReport"
    # Every orphan finding carries safe_to_fix=True.
    orphan_findings = [f for f in received_report.findings if f.category == "orphan"]
    assert orphan_findings, "expected at least one orphan finding in the report"
    for finding in orphan_findings:
        assert finding.safe_to_fix is True, (
            f"orphan finding {finding.message!r} must be safe_to_fix=True"
        )
    # No finding may have safe_to_fix=None or any non-bool value: the
    # deterministic shell must always set the flag explicitly.
    for finding in received_report.findings:
        assert isinstance(finding.safe_to_fix, bool), (
            f"finding {finding.message!r} safe_to_fix must be a bool, "
            f"got {type(finding.safe_to_fix).__name__}"
        )


def test_run_lint_uses_linter_agent_output(orch: Orchestrator) -> None:
    from lies.agents.linter import LintFinding, LintSeverity

    llm_report = LintReport(
        findings=[
            LintFinding(
                severity=LintSeverity.HIGH,
                category="contradiction",
                message="a vs b",
                pages=["wiki/a.md", "wiki/b.md"],
                safe_to_fix=False,
            )
        ],
        report_markdown="",
    )
    with (
        mock.patch.object(orch, "_call_linter", return_value=(llm_report, None)),
        mock.patch.object(orch, "_run_repair_agent"),
    ):
        report_md = orch.run_lint()
    assert "contradiction" in report_md


def test_run_lint_falls_back_to_shell_on_linter_failure(orch: Orchestrator) -> None:
    with (
        mock.patch.object(
            orch, "_call_linter", return_value=(LintReport(findings=[], report_markdown=""), "boom")
        ),
        mock.patch.object(orch, "_run_repair_agent"),
    ):
        report_md = orch.run_lint()
    assert "fallback" in report_md.lower() or "boom" in report_md


def test_run_lint_llm_empty_findings_keeps_shell_findings(orch: Orchestrator) -> None:
    orphan = orch.wiki.wiki_dir / "concepts" / "orphan.md"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text("---\ntitle: Orphan\ntype: concept\n---\n# Orphan\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=orch.wiki.data_root, check=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=orch.wiki.data_root, check=True)

    with (
        mock.patch.object(
            orch, "_call_linter", return_value=(LintReport(findings=[], report_markdown=""), None)
        ),
        mock.patch.object(orch, "_run_repair_agent"),
    ):
        report_md = orch.run_lint()
    assert "orphan" in report_md.lower()


def test_run_lint_dedup_collapses_duplicate_orphan(orch: Orchestrator) -> None:
    from lies.agents.linter import LintFinding, LintSeverity

    orphan = orch.wiki.wiki_dir / "concepts" / "orphan.md"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text("---\ntitle: Orphan\ntype: concept\n---\n# Orphan\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=orch.wiki.data_root, check=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=orch.wiki.data_root, check=True)

    llm_orphan = LintFinding(
        severity=LintSeverity.LOW,
        category="orphan",
        message="concepts/orphan.md has no inbound links.",
        pages=["concepts/orphan.md"],
        safe_to_fix=False,
    )
    llm_report = LintReport(findings=[llm_orphan], report_markdown="")

    with (
        mock.patch.object(orch, "_call_linter", return_value=(llm_report, None)),
        mock.patch.object(orch, "_run_repair_agent"),
    ):
        report_md = orch.run_lint()
    # Single occurrence; the LLM's safe_to_fix=False must NOT override the shell's True.
    assert report_md.count("concepts/orphan.md has no inbound links.") == 1
    assert "applied" not in report_md.lower()  # dry-run path
    # The merged report's orphan entry keeps safe_to_fix=True; visible by absence of skipped contradiction line.


def test_run_lint_apply_uses_merged_findings(orch: Orchestrator) -> None:
    from lies.agents.linter import LintFinding, LintSeverity

    orphan = orch.wiki.wiki_dir / "concepts" / "orphan.md"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text("---\ntitle: Orphan\ntype: concept\n---\n# Orphan\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=orch.wiki.data_root, check=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=orch.wiki.data_root, check=True)

    llm_contradiction = LintFinding(
        severity=LintSeverity.HIGH,
        category="contradiction",
        message="a vs b",
        pages=["wiki/a.md", "wiki/b.md"],
        safe_to_fix=False,
    )
    llm_report = LintReport(findings=[llm_contradiction], report_markdown="")

    captured: dict[str, object] = {}

    def fake_repair(prompt, deps=None, **_kwargs):  # type: ignore[no-untyped-def]
        captured["deps"] = deps
        return mock.Mock(output=RepairPlan(operations=[], rationale="noop", evidence=["f0"]))

    with (
        mock.patch.object(orch, "_call_linter", return_value=(llm_report, None)),
        mock.patch.object(orch._repair_agent, "run_sync", new=fake_repair),
        mock.patch.object(
            orch,
            "_apply_repair_plan",
            return_value=RepairReceipt(applied=[], skipped=[], deferred=[], errors=[]),
        ),
    ):
        orch.run_lint(apply=True)

    deps = captured.get("deps")
    assert isinstance(deps, RepairAgentDeps)
    categories = {f.category for f in deps.lint_report.findings}
    assert "orphan" in categories
    assert "contradiction" in categories
