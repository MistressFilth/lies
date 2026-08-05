"""Unit tests for Orchestrator._linter_agent + _call_linter."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

import pytest
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import RunContext

from lies.agents.linter import (
    LintDeps,
    LintFinding,
    LintReport,
    LintSeverity,
    _build_linter_prompt,
    linter_agent,
)
from lies.orchestrator import Orchestrator
from tests.conftest import make_wiki


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
    return Orchestrator(wiki=wiki, model="test")


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
    (orch.wiki.wiki_dir / "concepts").mkdir(exist_ok=True)
    (orch.wiki.wiki_dir / "concepts" / "alpha.md").write_text(
        "---\ntitle: Alpha\ntype: concept\n---\n# Alpha\nbody\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=orch.wiki.data_root, check=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=orch.wiki.data_root, check=True)

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
    assert deps.wiki_root == str(orch.wiki.data_root)


def test_call_linter_returns_empty_on_failure(orch: Orchestrator) -> None:
    with mock.patch.object(
        type(orch._linter_agent), "run_sync", side_effect=RuntimeError("model offline")
    ):
        report, fallback = orch._call_linter()
    assert report.findings == []
    assert fallback is not None
    assert "RuntimeError" in fallback
    assert "model offline" in fallback


def test_build_linter_prompt_includes_page_texts() -> None:
    """The linter's system-prompt callable must render every page's body
    into the prompt the model sees. Without this, pydantic-ai deps are
    only host-side ``RunContext`` data and the model has no way to
    read the wiki.
    """
    deps = LintDeps(
        page_texts={
            "concepts/alpha.md": "---\ntitle: Alpha\n---\n# Alpha\n\nbody-A\n",
            "concepts/beta.md": "---\ntitle: Beta\n---\n# Beta\n\nbody-B\n",
        },
        wiki_root="/tmp/wiki",
    )
    # The callable accepts a RunContext; build a synthetic one with just
    # the deps attribute the callable reads.
    ctx = RunContext(
        deps=deps,
        model=TestModel(),
        usage=None,  # type: ignore[arg-type]
        prompt="lint",
    )
    prompt = _build_linter_prompt(ctx)
    # Static instructions preserved.
    assert "contradiction" in prompt
    # Wiki root surfaced for reference.
    assert "/tmp/wiki" in prompt
    # Both page paths and bodies rendered.
    assert "concepts/alpha.md" in prompt
    assert "body-A" in prompt
    assert "concepts/beta.md" in prompt
    assert "body-B" in prompt


def test_linter_agent_registers_dynamic_system_prompt() -> None:
    """The linter agent must register ``_build_linter_prompt`` as a
    system-prompt callable so pydantic-ai renders deps into the prompt
    at run time. Without the registration, the model only sees the
    static prompt + the user message and cannot read any page.
    """
    agent: object = linter_agent(model=TestModel())
    # pydantic-ai stores system-prompt callables on the agent under
    # ``_system_prompt_functions`` (mutable list). Assert the linter's
    # callable is registered.
    sp_funcs = getattr(agent, "_system_prompt_functions", [])
    registered = [getattr(f, "function", f) for f in sp_funcs]
    assert _build_linter_prompt in registered, (
        f"_build_linter_prompt must be registered as a system-prompt callable, "
        f"got {[type(f).__name__ for f in registered]}"
    )
