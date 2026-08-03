"""Unit tests for Orchestrator._linter_agent + _call_linter."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

import pytest

from lies.agents.linter import LintDeps, LintFinding, LintReport, LintSeverity
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


def _contradiction_report() -> LintReport:
    return LintReport(
        findings=[
            LintFinding(
                severity=LintSeverity.HIGH,
                category="contradiction",
                message="pages disagree",
                pages=["concepts/a.md", "concepts/b.md"],
                safe_to_fix=False,
            )
        ],
        report_markdown="",
    )


def test_orchestrator_has_linter_agent(orch: Orchestrator) -> None:
    """The orchestrator constructs its own linter sub-agent (mirrors _repair_agent)."""
    assert hasattr(orch, "_linter_agent")
    assert orch._linter_agent is not None


def test_call_linter_returns_report_on_success(orch: Orchestrator) -> None:
    with mock.patch.object(
        type(orch._linter_agent),
        "run_sync",
        return_value=mock.Mock(output=_contradiction_report()),
    ):
        report, fallback = orch._call_linter()
    assert fallback is None
    assert any(f.category == "contradiction" for f in report.findings)


def test_call_linter_passes_page_texts_in_deps(orch: Orchestrator) -> None:
    """_call_linter collects every wiki page into ``LintDeps.page_texts``
    so the LLM can read the wiki without tool calls.
    """
    # Seed a page so the wiki has something to read.
    (orch.layout.wiki_dir / "concepts").mkdir(exist_ok=True)
    (orch.layout.wiki_dir / "concepts" / "alpha.md").write_text(
        "---\ntitle: Alpha\ntype: concept\n---\n# Alpha\nbody\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=orch.layout.root, check=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=orch.layout.root, check=True)

    captured: dict[str, object] = {}

    def fake_run_sync(prompt: str, deps: LintDeps | None = None, **_kwargs: object):  # type: ignore[no-untyped-def]
        captured["deps"] = deps
        return mock.Mock(output=_contradiction_report())

    with mock.patch.object(orch._linter_agent, "run_sync", side_effect=fake_run_sync):
        orch._call_linter()

    deps = captured.get("deps")
    assert isinstance(deps, LintDeps), f"linter agent must receive LintDeps, got {type(deps)!r}"
    assert "concepts/alpha.md" in deps.page_texts, (
        f"page_texts must include wiki-dir-relative alpha.md, got {sorted(deps.page_texts)!r}"
    )
    assert "body" in deps.page_texts["concepts/alpha.md"]
    assert deps.wiki_root == str(orch.layout.root)


def test_call_linter_returns_empty_on_failure(orch: Orchestrator) -> None:
    with mock.patch.object(
        type(orch._linter_agent), "run_sync", side_effect=RuntimeError("model offline")
    ):
        report, fallback = orch._call_linter()
    assert report.findings == []
    assert fallback is not None
    assert "RuntimeError" in fallback
    assert "model offline" in fallback
