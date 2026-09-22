"""Unit tests for Orchestrator._linter_agent + _call_linter (N2)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

import pytest

from lies.agents.linter import LintDeps, LintFinding, LintReport, LintSeverity
from lies.orchestrator import Orchestrator
from tests.conftest import make_wiki, models_for_tests


@pytest.fixture
def orch(tmp_path: Path) -> Orchestrator:
    root = tmp_path / "wiki"
    for sub in ("wiki", "raw"):
        (root / sub).mkdir(parents=True)
    (root / "wiki" / "index.md").write_text("# Index\n", encoding="utf-8")
    wiki = make_wiki(name="linter", data_root=root)
    wiki.config_root.mkdir(parents=True, exist_ok=True)
    (wiki.config_root / "schema.md").write_text("## Page types\n- concept\n", encoding="utf-8")
    subprocess.run(["git", "init", "--initial-branch=main", str(root)], check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True)
    return Orchestrator(wiki=wiki, models=models_for_tests("test"))


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


def test_call_linter_passes_marker_deps(orch: Orchestrator) -> None:
    """N2: ``_call_linter`` constructs a marker ``LintDeps()`` and dispatches
    with it. The pre-N2 surface (``page_texts`` / ``wiki_root`` on deps)
    was retired; pages are pulled via tool calls instead. Tool wiring is
    exercised by ``tests/unit/agents/test_linter_tools.py``.
    """
    captured: dict[str, object] = {}

    def fake_run_sync(prompt: str, deps: LintDeps | None = None, **_kwargs: object):  # type: ignore[no-untyped-def]
        captured["deps"] = deps
        return mock.Mock(output=_contradiction_report())

    with mock.patch.object(orch._linter_agent, "run_sync", side_effect=fake_run_sync):
        orch._call_linter()

    deps = captured.get("deps")
    assert isinstance(deps, LintDeps), f"linter agent must receive LintDeps, got {type(deps)!r}"
    # N2 marker: zero fields beyond what dataclass adds.
    assert len(deps.__dataclass_fields__) == 0  # type: ignore[attr-defined]


def test_call_linter_returns_empty_on_failure(orch: Orchestrator) -> None:
    with mock.patch.object(
        type(orch._linter_agent), "run_sync", side_effect=RuntimeError("model offline")
    ):
        report, fallback = orch._call_linter()
    assert report.findings == []
    assert fallback is not None
    assert "RuntimeError" in fallback
    assert "model offline" in fallback
