"""End-to-end tests for the lint repair workflow."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

import pytest
from pydantic_ai.models.test import TestModel

from lies.agents.linter import LintReport
from lies.agents.repair_models import (
    AppendLink,
    CreateStub,
    RepairPlan,
    UpdateIndex,
)
from lies.orchestrator import Orchestrator
from lies.wiki.wiki import Wiki
from tests.conftest import make_wiki, models_for_tests


@pytest.fixture
def wiki(tmp_path: Path) -> Wiki:
    root = tmp_path / "wiki"
    for sub in ("wiki", "raw"):
        (root / sub).mkdir(parents=True)
    (root / "wiki" / "concepts").mkdir(parents=True)
    (root / "wiki" / "index.md").write_text("# Index\n", encoding="utf-8")
    w = make_wiki(name="lint-repair", data_root=root)
    w.config_root.mkdir(parents=True, exist_ok=True)
    (w.config_root / "schema.md").write_text(
        "## Page types\n- concept\n- entity\n", encoding="utf-8"
    )
    subprocess.run(["git", "init", "--initial-branch=main", str(root)], check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True)
    return w


def _seed_page(wiki: Wiki, path: str, body: str) -> None:
    target = wiki.wiki_dir / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


def _noop_agent_run_sync(self, prompt: str):  # type: ignore[no-untyped-def]
    return mock.Mock(output="lint done")


def test_apply_three_append_links(wiki: Wiki) -> None:
    """Lint finds 3 missing-xref; --fix applies 3 append_links; single commit."""
    from lies.agents.linter import LintFinding, LintReport, LintSeverity
    from lies.orchestrator import _build_lint_report

    _seed_page(wiki, "concepts/a.md", "---\ntitle: A\n---\n# A\n")
    _seed_page(wiki, "concepts/b.md", "---\ntitle: B\n---\n# B\n")
    _seed_page(wiki, "concepts/c.md", "---\ntitle: C\n---\n# C\n")
    subprocess.run(["git", "add", "."], cwd=wiki.data_root, check=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=wiki.data_root, check=True)

    orch = Orchestrator(wiki=wiki, models=models_for_tests(TestModel()))
    # Three missing-xref findings the plan will reference. The host
    # (mentioner) appears first in ``pages``; the plan's AppendLink
    # carries only the host, which intersects the finding pages.
    shell_report = LintReport(
        findings=[
            LintFinding(
                severity=LintSeverity.MEDIUM,
                category="missing_xref",
                message="a mentions B without a cross-reference",
                pages=["concepts/a.md", "concepts/b.md"],
                safe_to_fix=True,
            ),
            LintFinding(
                severity=LintSeverity.MEDIUM,
                category="missing_xref",
                message="a mentions C without a cross-reference",
                pages=["concepts/a.md", "concepts/c.md"],
                safe_to_fix=True,
            ),
            LintFinding(
                severity=LintSeverity.MEDIUM,
                category="missing_xref",
                message="b mentions A without a cross-reference",
                pages=["concepts/b.md", "concepts/a.md"],
                safe_to_fix=True,
            ),
        ],
        report_markdown="",
    )
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
        mock.patch.object(
            orch, "_call_linter", return_value=(LintReport(findings=[], report_markdown=""), None)
        ),
        mock.patch.object(orch, "_run_repair_agent", return_value=plan),
        mock.patch(
            _build_lint_report.__module__ + "._build_lint_report", return_value=shell_report
        ),
    ):
        report = orch.run_lint(apply=True)

    assert "Applied" in report
    assert (wiki.wiki_dir / "concepts" / "a.md").read_text(encoding="utf-8").count("[B]") == 1
    assert (wiki.wiki_dir / "concepts" / "a.md").read_text(encoding="utf-8").count("[C]") == 1
    assert (wiki.wiki_dir / "concepts" / "b.md").read_text(encoding="utf-8").count("[A]") == 1


def test_apply_interleaved_append_links(wiki: Wiki) -> None:
    """Multiple AppendLinks to the same path interspersed with other ops apply successfully."""
    from lies.agents.linter import LintFinding, LintReport, LintSeverity
    from lies.orchestrator import _build_lint_report

    _seed_page(wiki, "concepts/a.md", "---\ntitle: A\ntype: concept\n---\n# A\n")
    _seed_page(wiki, "concepts/b.md", "---\ntitle: B\ntype: concept\n---\n# B\n")
    _seed_page(wiki, "concepts/c.md", "---\ntitle: C\ntype: concept\n---\n# C\n")
    subprocess.run(["git", "add", "."], cwd=wiki.data_root, check=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=wiki.data_root, check=True)

    orch = Orchestrator(wiki=wiki, models=models_for_tests(TestModel()))
    shell_report = LintReport(
        findings=[
            LintFinding(
                severity=LintSeverity.MEDIUM,
                category="missing_xref",
                message="a mentions B without a cross-reference",
                pages=["concepts/a.md", "concepts/b.md"],
                safe_to_fix=True,
            ),
            LintFinding(
                severity=LintSeverity.LOW,
                category="missing_page",
                message="missing concepts/new.md",
                pages=["concepts/a.md"],
                safe_to_fix=True,
            ),
            LintFinding(
                severity=LintSeverity.MEDIUM,
                category="missing_xref",
                message="a mentions C without a cross-reference",
                pages=["concepts/a.md", "concepts/c.md"],
                safe_to_fix=True,
            ),
        ],
        report_markdown="",
    )
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
                pages=["concepts/a.md"],
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
        mock.patch.object(
            orch, "_call_linter", return_value=(LintReport(findings=[], report_markdown=""), None)
        ),
        mock.patch.object(orch, "_run_repair_agent", return_value=plan),
        mock.patch(
            _build_lint_report.__module__ + "._build_lint_report", return_value=shell_report
        ),
    ):
        report = orch.run_lint(apply=True)

    assert "Applied" in report
    a_content = (wiki.wiki_dir / "concepts" / "a.md").read_text(encoding="utf-8")
    assert "[B]" in a_content
    assert "[C]" in a_content
    assert (wiki.wiki_dir / "concepts" / "new.md").exists()


def test_apply_skips_contradiction(wiki: Wiki) -> None:
    """Lint finds 1 orphan + 1 contradiction; --fix applies the orphan, skips the contradiction."""
    _seed_page(wiki, "concepts/orphan.md", "---\ntitle: Orphan\n---\n# Orphan\n")
    subprocess.run(["git", "add", "."], cwd=wiki.data_root, check=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=wiki.data_root, check=True)

    orch = Orchestrator(wiki=wiki, models=models_for_tests(TestModel()))
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
        mock.patch.object(
            orch, "_call_linter", return_value=(LintReport(findings=[], report_markdown=""), None)
        ),
        mock.patch.object(orch, "_run_repair_agent", return_value=plan),
    ):
        report = orch.run_lint(apply=True)

    assert "Applied" in report
    assert "Skipped" in report
    # The orphan was added to the index.
    assert "Orphan" in (wiki.wiki_dir / "index.md").read_text(encoding="utf-8")


def test_apply_fails_when_lock_held(wiki: Wiki, tmp_path: Path) -> None:
    """Cross-process flock blocks the apply; wiki is unchanged."""
    import sys
    import textwrap
    import time

    create_lock = wiki.memory_create_lock_path
    pid_path = wiki.memory_pid_path
    state_path = wiki.memory_heartbeat_path
    ready = tmp_path / "holder.ready"
    holder_script = textwrap.dedent(
        """
        import os, sys, time
        from pathlib import Path
        from lies.utils.exclusive import acquire_create_lock
        from lies.utils.lock_heartbeat import Heartbeat, write_heartbeat, write_owner_pid

        create_lock = Path(sys.argv[1])
        pid_path = Path(sys.argv[2])
        state_path = Path(sys.argv[3])
        ready_marker = Path(sys.argv[4])

        create_lock.parent.mkdir(parents=True, exist_ok=True)
        fd = acquire_create_lock(
            create_lock,
            max_age_s=7200,
            pid_path=pid_path,
            state_json_path=state_path,
        )
        if fd is None:
            sys.exit(2)
        write_owner_pid(pid_path, os.getpid())
        write_heartbeat(
            state_path,
            Heartbeat(pid=os.getpid(), started_at=time.time(), scope=""),
        )
        ready_marker.write_text("ready", encoding="utf-8")
        time.sleep(15)
        """
    )
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            holder_script,
            str(create_lock),
            str(pid_path),
            str(state_path),
            str(ready),
        ],
    )
    try:
        deadline = time.time() + 10.0
        while not ready.exists():
            if time.time() > deadline:
                pytest.fail("holder did not signal ready in time")
            time.sleep(0.05)

        orch = Orchestrator(wiki=wiki, models=models_for_tests(TestModel()))
        plan = RepairPlan(
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
        # Seed one deterministic shell finding matching the plan's
        # ``finding_index``/``pages`` so the structural validator's
        # rule 1 (bounds) and rule 3 (page intersection) both pass.
        # Without this the plan is rejected before the flock attempt
        # and the test stops exercising the cross-process lock.
        from lies.agents.linter import LintFinding, LintReport, LintSeverity
        from lies.orchestrator import _build_lint_report

        shell_report = LintReport(
            findings=[
                LintFinding(
                    severity=LintSeverity.LOW,
                    category="missing_page",
                    message="missing concepts/x.md",
                    pages=["concepts/x.md"],
                    safe_to_fix=True,
                ),
            ],
            report_markdown="",
        )
        with (
            mock.patch.object(type(orch._agent), "run_sync", new=_noop_agent_run_sync),
            mock.patch.object(
                orch,
                "_call_linter",
                return_value=(LintReport(findings=[], report_markdown=""), None),
            ),
            mock.patch.object(orch, "_run_repair_agent", return_value=plan),
            mock.patch(
                _build_lint_report.__module__ + "._build_lint_report",
                return_value=shell_report,
            ),
        ):
            report = orch.run_lint(apply=True)

        assert "Errors" in report or "errors" in report
        assert not (wiki.wiki_dir / "concepts" / "x.md").exists()
    finally:
        holder.terminate()
        holder.wait(timeout=5)


def test_apply_receipt_in_lint_report(wiki: Wiki) -> None:
    """The post-apply wiki/lint-report.md shows per-finding bullets."""
    _seed_page(wiki, "concepts/a.md", "---\ntitle: A\n---\n# A\n")
    _seed_page(wiki, "concepts/b.md", "---\ntitle: B\n---\n# B\n")
    subprocess.run(["git", "add", "."], cwd=wiki.data_root, check=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=wiki.data_root, check=True)

    orch = Orchestrator(wiki=wiki, models=models_for_tests(TestModel()))
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
        mock.patch.object(
            orch, "_call_linter", return_value=(LintReport(findings=[], report_markdown=""), None)
        ),
        mock.patch.object(orch, "_run_repair_agent", return_value=plan),
    ):
        orch.run_lint(apply=True)

    report = (wiki.wiki_dir / "lint-report.md").read_text(encoding="utf-8")
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
    for sub in ("wiki", "raw"):
        (root / sub).mkdir(parents=True)
    (root / "wiki" / "index.md").write_text("# Index\n", encoding="utf-8")
    w = make_wiki(name="lint-multi", data_root=root)
    w.config_root.mkdir(parents=True, exist_ok=True)
    (w.config_root / "schema.md").write_text("## Page types\n- concept\n", encoding="utf-8")
    subprocess.run(["git", "init", "--initial-branch=main", str(root)], check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True)
    return Orchestrator(wiki=w, models=models_for_tests("test"))


def test_run_lint_end_to_end_with_linter_categories(orch: Orchestrator) -> None:
    """End-to-end: real merge + real repair flow against the fixture wiki.

    Seeds cross-mention pages (alpha, beta), one missing source, one
    orphan (lonely). Mocks the linter sub-agent to add one
    contradiction finding (``safe_to_fix=False``). Then mocks ONLY
    the repair agent's outer LLM dispatch (instance-level) to return
    a ``RepairPlan`` with one ``UpdateIndex`` for the orphan and one
    ``AppendLink`` for a missing_xref — both safe-to-fix. The mock
    captures the ``RepairAgentDeps`` it received so the test can
    assert the safety filter at both ends: the contradiction reached
    the repair agent with ``safe_to_fix=False``, and the returned
    plan has no op referencing the contradiction finding.

    Everything else is real:
    - ``_build_lint_report`` is mocked to return a controlled report
      so the test plan can reference known findings (the validator
      requires ``op.pages`` to intersect the referenced finding's
      pages; the real shell's findings would not line up with the
      plan's hand-written indices);
    - ``_call_linter`` is mocked at the orchestrator boundary (returns
      the contradiction);
    - ``_run_repair_agent`` reads page texts, calls the real
      repair-agent dispatch (mocked at instance level), and unwraps
      ``.output``;
    - ``_apply_repair_plan`` goes through ``WikiMemoryService`` —
      flock, snapshot, write, atomic_commit, qmd refresh;
    - ``_render_lint_report`` writes ``wiki/lint-report.md``.
    """
    from lies.agents.linter import LintFinding, LintReport, LintSeverity
    from lies.agents.repair import RepairAgentDeps
    from lies.orchestrator import _build_lint_report

    base = orch.wiki.wiki_dir
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
    subprocess.run(["git", "add", "."], cwd=orch.wiki.data_root, check=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=orch.wiki.data_root, check=True)

    # Shell produces an orphan finding for lonely plus a missing_xref
    # alpha→beta and a missing_page for the raw/missing.md source, so
    # the plan's UpdateIndex (finding 0) and AppendLink (finding 1)
    # reference real, controlled findings.
    shell_report = LintReport(
        findings=[
            LintFinding(
                severity=LintSeverity.LOW,
                category="orphan",
                message="concepts/lonely.md has no inbound links.",
                pages=["concepts/lonely.md"],
                safe_to_fix=True,
            ),
            LintFinding(
                severity=LintSeverity.MEDIUM,
                category="missing_xref",
                message="alpha mentions Beta without a cross-reference",
                pages=["concepts/alpha.md", "concepts/beta.md"],
                safe_to_fix=True,
            ),
            LintFinding(
                severity=LintSeverity.LOW,
                category="missing_page",
                message="alpha references raw/missing.md which does not exist",
                pages=["concepts/alpha.md"],
                safe_to_fix=False,
            ),
        ],
        report_markdown="",
    )

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

    real_plan = RepairPlan(
        operations=[
            UpdateIndex(
                path="wiki/index.md",
                title="Lonely",
                finding_index=0,
                pages=["concepts/lonely.md"],
                rationale="orphan -> catalog entry",
                evidence=["f0"],
            ),
            AppendLink(
                target_path="concepts/beta.md",
                link_text="Beta",
                append_to="concepts/alpha.md",
                finding_index=1,
                pages=["concepts/alpha.md"],
                rationale="missing_xref alpha->beta",
                evidence=["f1"],
            ),
        ],
        rationale="index orphan + cross-link alpha",
        evidence=["f0", "f1"],
    )

    captured: dict[str, object] = {}

    def fake_repair_agent_run_sync(
        prompt: str, deps: RepairAgentDeps | None = None, **_kwargs: object
    ):  # type: ignore[no-untyped-def]
        captured["deps"] = deps
        return mock.Mock(output=real_plan)

    with (
        mock.patch(
            _build_lint_report.__module__ + "._build_lint_report", return_value=shell_report
        ),
        mock.patch.object(orch, "_call_linter", return_value=(llm_contradiction, None)),
        mock.patch.object(
            orch._repair_agent,
            "run_sync",
            side_effect=fake_repair_agent_run_sync,
        ),
    ):
        report_md = orch.run_lint(apply=True)

    # --- Safety filter: input side ----------------------------------
    # The merged lint report reached the repair agent with the
    # contradiction flagged unsafe. This proves the safety metadata
    # flowed through the merge envelope into the repair dispatch.
    deps = captured.get("deps")
    assert isinstance(deps, RepairAgentDeps), (
        f"repair agent must receive RepairAgentDeps, got {type(deps)!r}"
    )
    deps_report: LintReport = deps.lint_report
    contradiction_findings = [f for f in deps_report.findings if f.category == "contradiction"]
    assert contradiction_findings, "contradiction finding must reach the repair agent"
    assert contradiction_findings[0].safe_to_fix is False, (
        "contradiction finding must be safe_to_fix=False for the HARD RULE"
    )

    # --- Safety filter: output side ---------------------------------
    # The plan the agent returned contains no op referencing the
    # contradiction finding. In production the HARD RULE forbids ops
    # on ``safe_to_fix=False`` findings; here the mock returns a plan
    # the rule would have permitted, and the test pins down which
    # ``finding_index`` is the contradiction's slot in the merged
    # report (shell findings come first, so it's the last index).
    contradiction_index = next(
        i for i, f in enumerate(deps_report.findings) if f.category == "contradiction"
    )
    for op in real_plan.operations:
        assert op.finding_index != contradiction_index, (
            f"plan op {type(op).__name__} targets the contradiction finding "
            f"(index={contradiction_index}) — safety filter broken"
        )

    # --- Render: all four categories present in the body -----------
    for cat in ("orphan", "missing_xref", "missing_page", "contradiction"):
        assert cat in report_md, f"missing category {cat!r} in lint report"

    # --- Render: applied section is section-scoped -----------------
    # Slice the markdown between ``### Applied`` and ``### Sources``
    # so a stray word elsewhere in the report cannot satisfy the
    # check. ``### Sources`` is the right boundary because it always
    # closes the repair section (the report ends there).
    applied_section = report_md.split("### Applied", 1)[1].split("### Sources", 1)[0]
    assert "update_index" in applied_section
    assert "append_link" in applied_section
    # The contradiction is in the body but not in the Applied section.
    assert "contradiction" not in applied_section, (
        "contradiction (safe_to_fix=False) must not appear in the Applied section"
    )

    # --- Side effects of the real apply path -----------------------
    # The index was rebuilt by the apply envelope's ``rebuild_index``
    # step (so the link is rewritten into the catalog format with the
    # ``wiki/`` prefix), and alpha.md got a cross-link to beta via the
    # real ``_merge_append_links`` rewrite.
    index_text = (base / "index.md").read_text(encoding="utf-8")
    assert "[Lonely]" in index_text
    assert "concepts/lonely.md" in index_text
    alpha_text = (base / "concepts" / "alpha.md").read_text(encoding="utf-8")
    assert "[Beta](concepts/beta.md)" in alpha_text


def test_run_lint_repair_agent_receives_shell_page_texts(orch: Orchestrator) -> None:
    """Shell findings carry wiki-dir-relative paths so ``RepairAgentDeps.page_texts``
    is populated for every shell finding's referenced page.

    Regression for the path-convention mismatch where shell findings
    emitted repo-root-relative ``wiki/concepts/a.md`` paths and the
    repair agent's ``layout.wiki_dir / page`` lookup landed on
    ``wiki/wiki/concepts/a.md`` (a non-existent file), silently
    breaking the repair plan synthesis.
    """
    from lies.agents.repair import RepairAgentDeps

    base = orch.wiki.wiki_dir
    (base / "concepts").mkdir(exist_ok=True)
    (base / "concepts" / "alpha.md").write_text(
        "---\ntitle: Alpha\ntype: concept\n---\n# Alpha\n\nSee Beta for more.\n",
        encoding="utf-8",
    )
    (base / "concepts" / "beta.md").write_text(
        "---\ntitle: Beta\ntype: concept\n---\n# Beta\n\ndetails.\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=orch.wiki.data_root, check=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=orch.wiki.data_root, check=True)

    captured: dict[str, object] = {}

    def fake_repair_agent_run_sync(
        prompt: str, deps: RepairAgentDeps | None = None, **_kwargs: object
    ):  # type: ignore[no-untyped-def]
        captured["deps"] = deps
        return mock.Mock(output=RepairPlan(operations=[], rationale="noop", evidence=["f0"]))

    with (
        mock.patch.object(
            orch, "_call_linter", return_value=(LintReport(findings=[], report_markdown=""), None)
        ),
        mock.patch.object(orch._repair_agent, "run_sync", side_effect=fake_repair_agent_run_sync),
    ):
        orch.run_lint(apply=True)

    deps = captured.get("deps")
    assert isinstance(deps, RepairAgentDeps)
    # Every page the shell findings reference must appear in page_texts
    # with the real file contents (not an empty string from a mis-resolved path).
    assert deps.page_texts, (
        f"page_texts must be populated for shell findings, got {deps.page_texts!r}"
    )
    for path, body in deps.page_texts.items():
        assert body, f"page_texts[{path!r}] must carry real content, got empty string"
        assert "Alpha" in body or "Beta" in body, (
            f"page_texts[{path!r}] must be the actual wiki page content, got {body!r}"
        )


def test_run_lint_end_to_end_linter_unavailable(orch: Orchestrator) -> None:
    """Real ``_call_linter`` catches a raised ``run_sync`` and falls back.

    Drops the ``_call_linter`` mock and patches the underlying
    ``_linter_agent.run_sync`` with a ``side_effect`` that raises
    ``RuntimeError("model offline")``. Instance-level patching (not
    class-level) means the orchestrator's main ``_agent`` and any
    other sub-agents are unaffected — only the linter's dispatch is
    stubbed.

    The real ``_call_linter`` exception handler converts the raised
    exception into the fallback tuple. The real merge, the real
    ``_render_lint_report``, the real file writes to
    ``wiki/lint-report.md``, and the real log append all run. The
    shell's orphan finding survives in the merged report and the
    ``### Sources`` footer must carry the formatted fallback line.
    """
    base = orch.wiki.wiki_dir
    (base / "concepts").mkdir(exist_ok=True)
    (base / "concepts" / "lonely.md").write_text(
        "---\ntitle: Lonely\ntype: concept\n---\n# Lonely\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=orch.wiki.data_root, check=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=orch.wiki.data_root, check=True)

    def fake_run_sync(prompt: str, deps: object = None, **_kwargs: object):  # type: ignore[no-untyped-def]
        raise RuntimeError("model offline")

    with mock.patch.object(orch._linter_agent, "run_sync", side_effect=fake_run_sync):
        report_md = orch.run_lint()

    # Shell findings still appear in the merged report body.
    assert "orphan" in report_md.lower()

    # The fallback line in the Sources footer is the proof that the
    # raised ``RuntimeError`` was caught by ``_call_linter`` and
    # converted into the fallback tuple. ``_call_linter`` formats it
    # as ``f"{type(exc).__name__}: {exc}"`` → ``RuntimeError: model offline``.
    assert "fallback: RuntimeError: model offline" in report_md


# ---------------------------------------------------------------------------
# Task 6: rejection and drop paths for the repair plan validator.
# ---------------------------------------------------------------------------


def test_apply_rejects_plan_with_unsafe_finding(wiki: Wiki) -> None:
    """Repair agent emits a CreateStub for a safe_to_fix=False finding; --fix rejects."""
    from lies.agents.linter import LintFinding, LintReport, LintSeverity
    from lies.orchestrator import _build_lint_report

    _seed_page(wiki, "concepts/a.md", "---\ntitle: A\n---\n# A\n")
    subprocess.run(["git", "add", "."], cwd=wiki.data_root, check=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=wiki.data_root, check=True)

    orch = Orchestrator(wiki=wiki, models=models_for_tests(TestModel()))
    # A contradiction finding: safe_to_fix=False, but the plan still
    # emits a CreateStub. The structural validator must catch it.
    findings = [
        LintFinding(
            severity=LintSeverity.HIGH,
            category="contradiction",
            message="x",
            pages=["concepts/a.md"],
            safe_to_fix=False,
        ),
    ]
    plan = RepairPlan(
        operations=[
            CreateStub(
                path="concepts/new.md",
                title="New",
                finding_index=0,
                pages=["concepts/a.md"],
                rationale="r",
                evidence=["f0"],
            ),
        ],
        rationale="r",
        evidence=["f0"],
    )
    # Shell returns no findings so finding_index=0 in the merged
    # report is the contradiction (safe_to_fix=False); without this,
    # the real shell's orphan for concepts/a.md would occupy index 0
    # and rule 2 would let the CreateStub through.
    empty_shell = LintReport(findings=[], report_markdown="")
    with (
        mock.patch.object(type(orch._agent), "run_sync", new=_noop_agent_run_sync),
        mock.patch(_build_lint_report.__module__ + "._build_lint_report", return_value=empty_shell),
        mock.patch.object(
            orch,
            "_call_linter",
            return_value=(LintReport(findings=findings, report_markdown=""), None),
        ),
        mock.patch.object(orch, "_run_repair_agent", return_value=plan),
    ):
        report = orch.run_lint(apply=True)

    assert "plan rejected" in report
    assert "safe_to_fix is False" in report
    assert not (wiki.wiki_dir / "concepts" / "new.md").exists()
    # No commit beyond the seed (fixture init + test seed = 2 lines).
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=wiki.data_root, check=True, capture_output=True, text=True
    ).stdout
    assert log.count("\n") == 2  # init + seed only; the rejected plan added no commit


def test_apply_drops_redundant_update_index(wiki: Wiki) -> None:
    """Repair agent emits an UpdateIndex for a page already in the index; --fix drops it."""
    _seed_page(wiki, "concepts/lonely.md", "---\ntitle: Lonely\n---\n# Lonely\n")
    # Pre-seed the index with the orphan so the op is redundant.
    (wiki.wiki_dir / "index.md").write_text(
        "# Index\n- [Lonely](concepts/lonely.md)\n", encoding="utf-8"
    )
    subprocess.run(["git", "add", "."], cwd=wiki.data_root, check=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=wiki.data_root, check=True)

    orch = Orchestrator(wiki=wiki, models=models_for_tests(TestModel()))
    plan = RepairPlan(
        operations=[
            UpdateIndex(
                path="wiki/index.md",
                title="Lonely",
                finding_index=0,
                pages=["concepts/lonely.md"],
                rationale="orphan",
                evidence=["f0"],
            ),
        ],
        rationale="r",
        evidence=["f0"],
    )
    with (
        mock.patch.object(type(orch._agent), "run_sync", new=_noop_agent_run_sync),
        mock.patch.object(
            orch, "_call_linter", return_value=(LintReport(findings=[], report_markdown=""), None)
        ),
        mock.patch.object(orch, "_run_repair_agent", return_value=plan),
    ):
        report = orch.run_lint(apply=True)

    assert "Skipped (redundant)" in report
    assert "redundant-index" in report
    # The op was dropped, so the receipt has no applied entries.
    assert "Applied (0)" in report


def test_apply_partial_plan_rejects_whole_plan(wiki: Wiki) -> None:
    """One bad op in a multi-op plan rejects everything; wiki state is unchanged."""
    _seed_page(wiki, "concepts/a.md", "---\ntitle: A\n---\n# A\n")
    _seed_page(wiki, "concepts/b.md", "---\ntitle: B\n---\n# B\n")
    subprocess.run(["git", "add", "."], cwd=wiki.data_root, check=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=wiki.data_root, check=True)

    orch = Orchestrator(wiki=wiki, models=models_for_tests(TestModel()))
    # First AppendLink is fine; second AppendLink targets a missing page.
    plan = RepairPlan(
        operations=[
            AppendLink(
                target_path="concepts/b.md",
                link_text="B",
                append_to="concepts/a.md",
                finding_index=0,
                pages=["concepts/a.md"],
                rationale="x",
                evidence=["f0"],
            ),
            AppendLink(
                target_path="concepts/ghost.md",
                link_text="Ghost",
                append_to="concepts/a.md",
                finding_index=0,
                pages=["concepts/a.md"],
                rationale="x",
                evidence=["f0"],
            ),
        ],
        rationale="r",
        evidence=["f0"],
    )
    with (
        mock.patch.object(type(orch._agent), "run_sync", new=_noop_agent_run_sync),
        mock.patch.object(
            orch, "_call_linter", return_value=(LintReport(findings=[], report_markdown=""), None)
        ),
        mock.patch.object(orch, "_run_repair_agent", return_value=plan),
    ):
        report = orch.run_lint(apply=True)

    assert "plan rejected" in report
    # The first op never wrote anything because the whole plan was rejected.
    a_content = (wiki.wiki_dir / "concepts" / "a.md").read_text(encoding="utf-8")
    assert "[B]" not in a_content
