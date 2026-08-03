"""End-to-end tests for the lint repair workflow."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

import pytest
from pydantic_ai.models.test import TestModel

from lies.agents.repair_models import (
    AppendLink,
    CreateStub,
    RepairPlan,
    RepairReceipt,
    UpdateIndex,
)
from lies.orchestrator import Orchestrator
from lies.wiki.layout import WikiLayout


@pytest.fixture
def wiki(tmp_path: Path) -> WikiLayout:
    root = tmp_path / "wiki"
    for sub in ("wiki", ".lies", "raw"):
        (root / sub).mkdir(parents=True)
    (root / "wiki" / "concepts").mkdir(parents=True)
    (root / "wiki" / "index.md").write_text("# Index\n", encoding="utf-8")
    (root / ".lies" / "schema.md").write_text(
        "## Page types\n- concept\n- entity\n", encoding="utf-8"
    )
    subprocess.run(["git", "init", "--initial-branch=main", str(root)], check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True)
    return WikiLayout(root)


def _seed_page(wiki: WikiLayout, path: str, body: str) -> None:
    target = wiki.wiki_dir / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


def _noop_agent_run_sync(self, prompt: str):  # type: ignore[no-untyped-def]
    return mock.Mock(output="lint done")


def test_apply_three_append_links(wiki: WikiLayout) -> None:
    """Lint finds 3 missing-xref; --fix applies 3 append_links; single commit."""
    _seed_page(wiki, "concepts/a.md", "---\ntitle: A\n---\n# A\n")
    _seed_page(wiki, "concepts/b.md", "---\ntitle: B\n---\n# B\n")
    _seed_page(wiki, "concepts/c.md", "---\ntitle: C\n---\n# C\n")
    subprocess.run(["git", "add", "."], cwd=wiki.root, check=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=wiki.root, check=True)

    orch = Orchestrator(wiki_root=wiki.root, model=TestModel())
    plan = RepairPlan(
        operations=[
            AppendLink(
                target_path="concepts/b.md",
                link_text="B",
                append_to="concepts/a.md",
                finding_index=0,
                pages=["concepts/a.md"],
                rationale="xref",
                evidence=["f0"],
            ),
            AppendLink(
                target_path="concepts/c.md",
                link_text="C",
                append_to="concepts/a.md",
                finding_index=1,
                pages=["concepts/a.md"],
                rationale="xref",
                evidence=["f1"],
            ),
            AppendLink(
                target_path="concepts/a.md",
                link_text="A",
                append_to="concepts/b.md",
                finding_index=2,
                pages=["concepts/b.md"],
                rationale="xref",
                evidence=["f2"],
            ),
        ],
        rationale="three xrefs",
        evidence=["f0", "f1", "f2"],
    )
    with (
        mock.patch.object(type(orch._agent), "run_sync", new=_noop_agent_run_sync),
        mock.patch.object(orch, "_run_repair_agent", return_value=plan),
    ):
        report = orch.run_lint(apply=True)

    assert "Applied" in report
    assert (wiki.wiki_dir / "concepts" / "a.md").read_text(encoding="utf-8").count("[B]") == 1
    assert (wiki.wiki_dir / "concepts" / "a.md").read_text(encoding="utf-8").count("[C]") == 1
    assert (wiki.wiki_dir / "concepts" / "b.md").read_text(encoding="utf-8").count("[A]") == 1


def test_apply_interleaved_append_links(wiki: WikiLayout) -> None:
    """Multiple AppendLinks to the same path interspersed with other ops apply successfully."""
    _seed_page(wiki, "concepts/a.md", "---\ntitle: A\ntype: concept\n---\n# A\n")
    _seed_page(wiki, "concepts/b.md", "---\ntitle: B\ntype: concept\n---\n# B\n")
    _seed_page(wiki, "concepts/c.md", "---\ntitle: C\ntype: concept\n---\n# C\n")
    subprocess.run(["git", "add", "."], cwd=wiki.root, check=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=wiki.root, check=True)

    orch = Orchestrator(wiki_root=wiki.root, model=TestModel())
    # First AppendLink on a.md, then CreateStub on concepts/new.md, then another AppendLink on a.md.
    plan = RepairPlan(
        operations=[
            AppendLink(
                target_path="concepts/b.md",
                link_text="B",
                append_to="concepts/a.md",
                finding_index=0,
                pages=["concepts/a.md"],
                rationale="xref",
                evidence=["f0"],
            ),
            CreateStub(
                path="concepts/new.md",
                title="New",
                finding_index=1,
                pages=[],
                rationale="new",
                evidence=["f1"],
            ),
            AppendLink(
                target_path="concepts/c.md",
                link_text="C",
                append_to="concepts/a.md",
                finding_index=2,
                pages=["concepts/a.md"],
                rationale="xref",
                evidence=["f2"],
            ),
        ],
        rationale="interleaved",
        evidence=["f0", "f1", "f2"],
    )
    with (
        mock.patch.object(type(orch._agent), "run_sync", new=_noop_agent_run_sync),
        mock.patch.object(orch, "_run_repair_agent", return_value=plan),
    ):
        report = orch.run_lint(apply=True)

    assert "Applied" in report
    a_content = (wiki.wiki_dir / "concepts" / "a.md").read_text(encoding="utf-8")
    assert "[B]" in a_content
    assert "[C]" in a_content
    assert (wiki.wiki_dir / "concepts" / "new.md").exists()


def test_apply_skips_contradiction(wiki: WikiLayout) -> None:
    """Lint finds 1 orphan + 1 contradiction; --fix applies the orphan, skips the contradiction."""
    _seed_page(wiki, "concepts/orphan.md", "---\ntitle: Orphan\n---\n# Orphan\n")
    subprocess.run(["git", "add", "."], cwd=wiki.root, check=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=wiki.root, check=True)

    orch = Orchestrator(wiki_root=wiki.root, model=TestModel())
    plan = RepairPlan(
        operations=[
            UpdateIndex(
                path="wiki/index.md",
                title="Orphan",
                finding_index=0,
                pages=["concepts/orphan.md"],
                rationale="orphan",
                evidence=["f0"],
            ),
        ],
        rationale="index orphan",
        evidence=["f0"],
    )
    with (
        mock.patch.object(type(orch._agent), "run_sync", new=_noop_agent_run_sync),
        mock.patch.object(orch, "_run_repair_agent", return_value=plan),
    ):
        report = orch.run_lint(apply=True)

    assert "Applied" in report
    assert "Skipped" in report
    # The orphan was added to the index.
    assert "Orphan" in (wiki.wiki_dir / "index.md").read_text(encoding="utf-8")


def test_apply_fails_when_lock_held(wiki: WikiLayout, tmp_path: Path) -> None:
    """Cross-process flock blocks the apply; wiki is unchanged."""
    import sys
    import textwrap
    import time

    lock_path = wiki.root / ".lies" / "memory.lock"
    ready = tmp_path / "holder.ready"
    holder_script = textwrap.dedent(
        """
        import fcntl, sys, time
        from pathlib import Path
        lock_path = Path(sys.argv[1])
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = lock_path.open("w", encoding="utf-8")
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        Path(sys.argv[2]).write_text("ready", encoding="utf-8")
        time.sleep(15)
        fd.close()
        """
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", holder_script, str(lock_path), str(ready)],
    )
    try:
        deadline = time.time() + 10.0
        while not ready.exists():
            if time.time() > deadline:
                pytest.fail("holder did not signal ready in time")
            time.sleep(0.05)

        orch = Orchestrator(wiki_root=wiki.root, model=TestModel())
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
            mock.patch.object(type(orch._agent), "run_sync", new=_noop_agent_run_sync),
            mock.patch.object(orch, "_run_repair_agent", return_value=plan),
        ):
            report = orch.run_lint(apply=True)

        assert "Errors" in report or "errors" in report
        assert not (wiki.wiki_dir / "concepts" / "x.md").exists()
    finally:
        holder.terminate()
        holder.wait(timeout=5)


def test_apply_receipt_in_lint_report(wiki: WikiLayout) -> None:
    """The post-apply wiki/lint-report.md shows per-finding bullets."""
    _seed_page(wiki, "concepts/a.md", "---\ntitle: A\n---\n# A\n")
    _seed_page(wiki, "concepts/b.md", "---\ntitle: B\n---\n# B\n")
    subprocess.run(["git", "add", "."], cwd=wiki.root, check=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=wiki.root, check=True)

    orch = Orchestrator(wiki_root=wiki.root, model=TestModel())
    plan = RepairPlan(
        operations=[
            AppendLink(
                target_path="concepts/b.md",
                link_text="B",
                append_to="concepts/a.md",
                finding_index=0,
                pages=["concepts/a.md"],
                rationale="xref",
                evidence=["f0"],
            ),
        ],
        rationale="r",
        evidence=["f0"],
    )
    with (
        mock.patch.object(type(orch._agent), "run_sync", new=_noop_agent_run_sync),
        mock.patch.object(orch, "_run_repair_agent", return_value=plan),
    ):
        orch.run_lint(apply=True)

    report = wiki.lint_report_path.read_text(encoding="utf-8")
    assert "Applied" in report
    assert "append_link" in report
    assert "concepts/a.md" in report


# ---------------------------------------------------------------------------
# Task 6: end-to-end coverage for the merged multi-category lint report.
# ---------------------------------------------------------------------------


@pytest.fixture
def orch(tmp_path: Path) -> Orchestrator:
    """A bare orchestrator fixture local to this file.

    Mirrors the fixture in ``tests/unit/test_orchestrator_lint.py``: a
    fresh wiki root with a stub ``index.md`` and ``schema.md``, an
    initial git commit, and an ``Orchestrator`` bound to it. Tests in
    this file that need to mutate the wiki tree should take this
    fixture rather than constructing ``Orchestrator`` inline, so the
    fixture is the single source of truth for the harness bootstrap.
    """
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


def test_run_lint_end_to_end_with_linter_categories(orch: Orchestrator) -> None:
    """End-to-end: cross-mentions + missing source + orphan + LLM contradiction.

    Seed two pages that mention each other's titles without linking
    (cross-mentions), one page that cites a non-existent source
    (missing source), and one orphan page (no inbound links). Mock
    the linter sub-agent to add one contradiction finding. The merged
    report must carry all four categories, and ``apply=True`` must
    route the safe-to-fix findings through the repair flow while
    leaving the contradiction surfaced as a finding (never auto-fixed).
    """
    from lies.agents.linter import LintFinding, LintReport, LintSeverity

    base = orch.layout.wiki_dir
    (base / "concepts").mkdir(exist_ok=True)
    (base / "concepts" / "alpha.md").write_text(
        "---\ntitle: Alpha\ntype: concept\nsources:\n  - raw/missing.md\n---\n"
        "# Alpha\n\nSee Beta for more.\n",
        encoding="utf-8",
    )
    (base / "concepts" / "beta.md").write_text(
        "---\ntitle: Beta\ntype: concept\n---\n# Beta\n\nAlpha covers the basics.\n",
        encoding="utf-8",
    )
    (base / "concepts" / "lonely.md").write_text(
        "---\ntitle: Lonely\ntype: concept\n---\n# Lonely\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=orch.layout.root, check=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=orch.layout.root, check=True)

    llm_contradiction = LintReport(
        findings=[
            LintFinding(
                severity=LintSeverity.HIGH,
                category="contradiction",
                message="alpha vs beta disagree",
                pages=["wiki/concepts/alpha.md", "wiki/concepts/beta.md"],
                safe_to_fix=False,
            )
        ],
        report_markdown="",
    )

    fake_plan = RepairPlan(operations=[], rationale="noop for safe findings", evidence=["f0"])
    fake_receipt = RepairReceipt(applied=[], skipped=[], deferred=[], errors=[])

    with (
        mock.patch.object(type(orch._agent), "run_sync", return_value=mock.Mock(output="ok")),
        mock.patch.object(orch, "_call_linter", return_value=(llm_contradiction, None)),
        mock.patch.object(orch, "_run_repair_agent", return_value=fake_plan),
        mock.patch.object(orch, "_apply_repair_plan", return_value=fake_receipt),
    ):
        report_md = orch.run_lint(apply=True)

    # All four categories present in the merged report.
    for cat in ("orphan", "missing_xref", "missing_page", "contradiction"):
        assert cat in report_md, f"missing category {cat!r} in lint report"


def test_run_lint_end_to_end_linter_unavailable(orch: Orchestrator) -> None:
    """LLM unavailable; final report carries shell findings plus fallback line.

    Seed one orphan page. Mock ``_call_linter`` to return an empty
    ``LintReport`` paired with a non-None fallback reason. The merged
    report must carry the shell's orphan finding AND the ``- fallback:``
    line so the reader can see the LLM path degraded gracefully.
    """
    from lies.agents.linter import LintReport

    base = orch.layout.wiki_dir
    (base / "concepts").mkdir(exist_ok=True)
    (base / "concepts" / "lonely.md").write_text(
        "---\ntitle: Lonely\ntype: concept\n---\n# Lonely\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=orch.layout.root, check=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=orch.layout.root, check=True)

    with (
        mock.patch.object(type(orch._agent), "run_sync", return_value=mock.Mock(output="ok")),
        mock.patch.object(
            orch,
            "_call_linter",
            return_value=(
                LintReport(findings=[], report_markdown=""),
                "RuntimeError: model offline",
            ),
        ),
        mock.patch.object(orch, "_run_repair_agent"),
    ):
        report_md = orch.run_lint()

    assert "orphan" in report_md.lower()
    assert "fallback" in report_md.lower()
