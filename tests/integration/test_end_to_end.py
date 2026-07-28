"""End-to-end integration test using a fixture wiki.

Finding 11 — `lint` (CLI smoke) and `run_query` are not covered by an
integration test that drives the public `Orchestrator` API end-to-end.
This test exercises the full LIES flow on a real fixture wiki:

    1. Ingest — ``Orchestrator.run_ingest`` writes artifacts and creates
       exactly one atomic git commit.
    2. Query fallback — ``Orchestrator.run_query`` synthesizes a deterministic
       answer from ``wiki/index.md`` when qmd is unavailable.
    3. Lint — ``Orchestrator.run_lint`` writes ``wiki/lint-report.md`` and
       appends a parseable entry to ``wiki/log.md``.

The agent's LLM is mocked so the round-trip is deterministic. The
underlying ``_agent.run_sync`` is patched to drop a wiki page (mimicking
the page-writer + indexer sub-agents writing artifacts).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from lies.orchestrator import Orchestrator
from lies.qmd.cli import QmdNotInstalledError
from lies.query import synthesize_answer
from lies.schema import load_schema
from lies.wiki.layout import WikiLayout

FIXTURE = Path(__file__).parent.parent / "fixtures" / "sample-wiki"


# ---------------------------------------------------------------------------
# Fixture: a real git wiki with the sample corpus
# ---------------------------------------------------------------------------


@pytest.fixture
def wiki_copy(tmp_path: Path) -> Path:
    """Copy the fixture wiki to a tmp directory and init git there."""
    target = tmp_path / "wiki"
    shutil.copytree(FIXTURE, target)
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(target)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=target, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=target, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "add", "."], cwd=target, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "fixture"],
        cwd=target, check=True, capture_output=True,
    )
    return target


def _log_lines(repo: Path) -> list[str]:
    """Return ``[<sha> <subject>, ...]`` newest-first."""
    result = subprocess.run(
        ["git", "log", "--pretty=%H %s"],
        cwd=repo, capture_output=True, text=True, check=True,
    )
    return result.stdout.splitlines()


def _git_show_files(repo: Path, sha: str) -> list[str]:
    """Return the list of files changed in ``sha``."""
    result = subprocess.run(
        ["git", "show", "--name-only", "--pretty=format:", sha],
        cwd=repo, capture_output=True, text=True, check=True,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Smoke tests — wiki layout, schema, orchestrator construct
# ---------------------------------------------------------------------------


def test_layout_resolves(wiki_copy: Path) -> None:
    layout = WikiLayout(wiki_copy)
    assert layout.is_git_repo() is True
    assert layout.index_path.exists()
    assert layout.log_path.exists()


def test_schema_loads(wiki_copy: Path) -> None:
    layout = WikiLayout(wiki_copy)
    schema = load_schema(layout)
    assert "Page types" in schema or "page types" in schema


def test_orchestrator_constructs(wiki_copy: Path) -> None:
    orch = Orchestrator(wiki_root=wiki_copy, model="test")
    assert orch is not None
    assert orch.wiki_root == wiki_copy.resolve()


# ---------------------------------------------------------------------------
# Ingest — run_ingest writes artifacts + one atomic git commit
# ---------------------------------------------------------------------------


def test_run_ingest_writes_artifacts_and_one_commit(wiki_copy: Path) -> None:
    """A successful ingest leaves exactly one new commit with the new page.

    This is the load-bearing part of the contract: an ingest is
    all-or-nothing. The wiki never appears half-ingested.
    """
    orch = Orchestrator(wiki_root=wiki_copy, model="test")
    pre_log = _log_lines(wiki_copy)
    pre_count = len(pre_log)

    def fake_run_sync(self, prompt: str):  # type: ignore[no-untyped-def]
        # Mimic page-writer + indexer dropping artifacts into the wiki.
        page = wiki_copy / "wiki" / "entities" / "fixture-entity.md"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(
            "---\ntitle: Fixture\ntype: entity\n---\n# Fixture\n",
            encoding="utf-8",
        )
        return mock.Mock(output="ingested fixture-entity")

    with mock.patch.object(type(orch._agent), "run_sync", new=fake_run_sync):
        result = orch.run_ingest("raw/articles/sample-article.md")

    assert result == "ingested fixture-entity"

    # Exactly one new commit.
    post_log = _log_lines(wiki_copy)
    assert len(post_log) == pre_count + 1
    new_sha, _, new_msg = post_log[0].partition(" ")
    assert new_msg.startswith("ingest")

    # The new artifact is recorded in the new commit.
    touched = _git_show_files(wiki_copy, new_sha)
    assert any(p.endswith("entities/fixture-entity.md") for p in touched), (
        f"fixture-entity.md not in commit; got {touched!r}"
    )

    # No leftover stash.
    stash = subprocess.run(
        ["git", "stash", "list"],
        cwd=wiki_copy, capture_output=True, text=True, check=True,
    )
    assert stash.stdout.strip() == ""


# ---------------------------------------------------------------------------
# Query fallback — synthesize_answer from wiki/index.md
# ---------------------------------------------------------------------------


def test_run_query_falls_back_to_index_when_qmd_unavailable(
    wiki_copy: Path,
) -> None:
    """Query with no qmd installed reads from wiki/index.md and returns a
    SynthesizedAnswer whose fallback fields are populated correctly.
    """
    orch = Orchestrator(wiki_root=wiki_copy, model="test")
    answer = orch.run_query("How does Postgres handle concurrency?")

    if shutil.which("qmd") is None:
        # qmd unavailable → fallback path.
        assert answer.fallback_used is True
        assert answer.fallback_reason == "qmd_unavailable"
    else:
        # qmd happened to be installed; either path is acceptable as long
        # as the contract (answer is non-empty) is met.
        assert answer.answer

    # The answer is markdown; it includes the question heading and at
    # least one bullet for the read pages.
    assert "### " in answer.answer
    assert answer.citations, "expected at least one cited page"


def test_synthesizer_reads_index_pages(wiki_copy: Path) -> None:
    """Direct call to ``synthesize_answer`` exercises the index-driven path."""
    layout = WikiLayout(wiki_copy)

    def boom(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise QmdNotInstalledError("simulated: qmd unavailable")

    answer = synthesize_answer(
        "What is MVCC?",
        layout,
        qmd_search=boom,
    )
    assert answer.fallback_used is True
    assert answer.fallback_reason == "qmd_unavailable"
    # The index lists Postgres + MySQL entities; the synthesizer reads
    # the top-N pages referenced from index.md and cites them.
    assert any(
        "entities/postgres.md" in c or "entities/mysql.md" in c
        for c in answer.citations
    ), f"expected entity citations, got {answer.citations!r}"


# ---------------------------------------------------------------------------
# Lint — run_lint writes wiki/lint-report.md and appends to wiki/log.md
# ---------------------------------------------------------------------------


def test_run_lint_writes_lint_report_and_appends_log(wiki_copy: Path) -> None:
    """Lint writes a real artifact to wiki/lint-report.md and a log entry."""
    orch = Orchestrator(wiki_root=wiki_copy, model="test")

    def fake_run_sync(self, prompt: str):  # type: ignore[no-untyped-def]
        return mock.Mock(output="lint done")

    with mock.patch.object(type(orch._agent), "run_sync", new=fake_run_sync):
        report_md = orch.run_lint()

    layout = WikiLayout(wiki_copy)
    # Artifact 1: lint-report.md exists and has the expected header.
    assert layout.lint_report_path.exists()
    on_disk = layout.lint_report_path.read_text(encoding="utf-8")
    assert "Lint report" in on_disk
    assert on_disk == report_md

    # Artifact 2: log.md has a new "lint" entry.
    log_text = layout.log_path.read_text(encoding="utf-8")
    assert "lint" in log_text
    # The entry is parseable: starts with `## [<date>] lint | N findings`.
    assert any(
        line.startswith("## [") and " lint " in line
        for line in log_text.splitlines()
    ), f"no parseable lint log entry; got:\n{log_text!r}"


def test_run_lint_detects_orphan_pages(wiki_copy: Path) -> None:
    """An orphan page (not referenced from index.md or anywhere else) is
    surfaced as a lint finding."""
    # Drop a fresh orphan page; nothing else links to it.
    orphan = wiki_copy / "wiki" / "entities" / "orphan-page.md"
    orphan.write_text("# Orphan\n\nNo inbound links.\n", encoding="utf-8")

    orch = Orchestrator(wiki_root=wiki_copy, model="test")

    def fake_run_sync(self, prompt: str):  # type: ignore[no-untyped-def]
        return mock.Mock(output="lint done")

    with mock.patch.object(type(orch._agent), "run_sync", new=fake_run_sync):
        report_md = orch.run_lint()

    assert "orphan" in report_md
    assert "orphan-page.md" in report_md


# ---------------------------------------------------------------------------
# qmd error path
# ---------------------------------------------------------------------------


def test_qmd_update_raises_cleanly_when_not_installed(wiki_copy: Path) -> None:
    """If qmd is missing, qmd_update raises QmdNotInstalledError, not a crash."""
    if shutil.which("qmd") is not None:
        pytest.skip("qmd is installed; skipping not-installed test")
    from lies.qmd import qmd_update
    with pytest.raises(QmdNotInstalledError):
        qmd_update(wiki_copy)