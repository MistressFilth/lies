"""Tests for the LIES MCP server tools.

Each tool is tested by calling the decorated function directly after
registering the server module. The Orchestrator's agent ``run_sync``
is mocked so no real LLM call is made — same pattern as
``tests/integration/test_end_to_end.py``. The real
``Orchestrator.run_ingest``, ``Orchestrator.run_query``, and
``Orchestrator.run_lint`` methods are NOT mocked, so the
tool-to-orchestrator delegation is exercised end-to-end.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from lies import __version__
from lies.mcp.resolution import WikiRootError
from lies.mcp.server import (
    SynthesizedMcpAnswer,
    ingest_source,
    init_wiki,
    lint,
    mcp,
    query,
)
from lies.orchestrator import Orchestrator
from lies.schema.loader import load_default_schema


@pytest.fixture(autouse=True)
def _use_test_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force ``Orchestrator`` to use pydantic_ai's ``TestModel``.

    The tool bodies construct ``Orchestrator(wiki_root=layout.root)``
    with no ``model`` argument, so they pick up ``LIES_MODEL`` from the
    environment. We default it to ``"test"`` so the agent object can
    be built without the ``anthropic`` provider package installed.
    Individual tests may still override ``LIES_WIKI_ROOT`` etc.
    """
    monkeypatch.setenv("LIES_MODEL", "test")


# ---------------------------------------------------------------------------
# Helpers — same shape as ``tests/integration/test_end_to_end.py``
# ---------------------------------------------------------------------------


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
# init_wiki — bootstrap a new wiki
# ---------------------------------------------------------------------------


def test_init_wiki_creates_wiki_structure(tmp_path: Path) -> None:
    target = tmp_path / "new-wiki"
    out = init_wiki(str(target))
    assert f"lies {__version__}" not in out  # sanity — not the version string
    assert "Initialized" in out
    assert target.is_dir()
    assert (target / "wiki").is_dir()
    assert (target / "raw").is_dir()
    assert (target / ".lies").is_dir()
    assert (target / ".lies" / "schema.md").is_file()
    # Schema contents match the bundled default exactly.
    assert (target / ".lies" / "schema.md").read_text(
        encoding="utf-8"
    ) == load_default_schema()
    # Git is initialized and has at least the initial commit.
    assert (target / ".git").exists()
    log = _log_lines(target)
    assert log, "expected at least the initial commit"
    _sha, _, subject = log[0].partition(" ")
    assert subject.startswith("Initial commit")


def test_init_wiki_rejects_non_empty_target(tmp_path: Path) -> None:
    target = tmp_path / "existing"
    target.mkdir()
    (target / "stuff").write_text("x", encoding="utf-8")
    with pytest.raises(WikiRootError, match="not empty"):
        init_wiki(str(target))


def test_init_wiki_rejects_existing_file(tmp_path: Path) -> None:
    """An existing regular file is rejected with a structured WikiRootError.

    Without the ``is_dir()`` guard, ``any(target.iterdir())`` would
    raise ``NotADirectoryError`` on a file — leaking a low-level
    filesystem error instead of the structured contract error.
    """
    target = tmp_path / "existing-file"
    target.write_text("x", encoding="utf-8")
    with pytest.raises(WikiRootError, match="not a directory"):
        init_wiki(str(target))


# ---------------------------------------------------------------------------
# ingest_source — atomic ingest through the orchestrator
# ---------------------------------------------------------------------------


def test_ingest_source_returns_agent_output(sample_wiki) -> None:
    """ingest_source delegates to Orchestrator.run_ingest end-to-end.

    Mocks only the agent's ``run_sync`` (mimicking the page-writer +
    indexer sub-agents writing artifacts) so the real Orchestrator
    runs. Asserts the new atomic commit contains the artifact.
    """
    orch = Orchestrator(wiki_root=sample_wiki.root, model="test")
    pre_log = _log_lines(sample_wiki.root)

    def fake_run_sync(self, prompt: str):  # type: ignore[no-untyped-def]
        # Mimic page-writer + indexer dropping artifacts into the wiki.
        page = sample_wiki.root / "wiki" / "entities" / "fixture-entity.md"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(
            "---\ntitle: Fixture\ntype: entity\n---\n# Fixture\n",
            encoding="utf-8",
        )
        return mock.Mock(output="ingested fixture-entity")

    with mock.patch.object(type(orch._agent), "run_sync", new=fake_run_sync):
        out = ingest_source(
            "raw/articles/sample-article.md",
            wiki_root=str(sample_wiki.root),
        )

    assert out == "ingested fixture-entity"

    # Real Orchestrator ran: exactly one new commit with the new page.
    post_log = _log_lines(sample_wiki.root)
    assert len(post_log) == len(pre_log) + 1
    new_sha, _, new_msg = post_log[0].partition(" ")
    assert new_msg.startswith("ingest")
    touched = _git_show_files(sample_wiki.root, new_sha)
    assert any(p.endswith("entities/fixture-entity.md") for p in touched), (
        f"fixture-entity.md not in commit; got {touched!r}"
    )

    # The artifact on disk matches what the agent claimed to write.
    on_disk = (sample_wiki.root / "wiki" / "entities" / "fixture-entity.md").read_text(
        encoding="utf-8"
    )
    assert "Fixture" in on_disk


# ---------------------------------------------------------------------------
# query — extractive answer with structured fallback metadata
# ---------------------------------------------------------------------------


def test_query_returns_synthesized_mcp_answer(
    sample_wiki, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """query returns a SynthesizedMcpAnswer with the right three fields.

    Uses the real Orchestrator (``run_query`` is deterministic and
    does not invoke any LLM) so the test exercises the actual
    MCP → Orchestrator delegation. The fallback fields depend on
    whether qmd is installed; the contract is asserted for both
    branches.
    """
    monkeypatch.setenv("LIES_WIKI_ROOT", str(sample_wiki.root))

    result = query("What is MVCC?")

    assert isinstance(result, SynthesizedMcpAnswer)
    assert "### " in result.answer
    if shutil.which("qmd") is None:
        # qmd unavailable → fallback path.
        assert result.fallback_used is True
        assert result.fallback_reason == "qmd_unavailable"
    else:
        # qmd served the query; the contract is just "answer is non-empty".
        assert result.answer


def test_query_maps_empty_fallback_reason_to_none(
    sample_wiki, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When qmd serves the query, ``fallback_reason`` becomes ``None``.

    The internal ``SynthesizedAnswer`` uses an empty string to mean
    "fallback unused"; the MCP-facing ``SynthesizedMcpAnswer`` maps
    that to ``None`` so callers can use a simple ``is None`` check.

    ``run_query`` is deterministic and doesn't invoke the agent's
    ``run_sync`` — there's nothing to mock at that level. We patch
    the synthesizer's internal qmd-dispatch helper (an internal of
    ``Orchestrator.run_query``, not its public surface) to simulate
    the "qmd served the query" branch deterministically.
    """
    monkeypatch.setenv("LIES_WIKI_ROOT", str(sample_wiki.root))

    from lies.query import synthesizer as q_syn
    from lies.query.synthesizer import _PageRead

    def fake_dispatch(fn, layout, question, top_n):  # type: ignore[no-untyped-def]
        # Pretend qmd returned one readable hit.
        return [_PageRead(
            rel_path="entities/postgres.md",
            title="Postgres",
            excerpt="PostgreSQL uses MVCC.",
        )]

    with mock.patch.object(q_syn, "_qmd_search_dispatch", new=fake_dispatch):
        result = query("What is MVCC?")

    assert isinstance(result, SynthesizedMcpAnswer)
    assert result.fallback_used is False
    assert result.fallback_reason is None


def test_query_propagates_fallback_reason(
    sample_wiki, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When qmd is unavailable, fallback_used and fallback_reason surface.

    Patches the synthesizer's qmd-dispatch helper (an internal of
    ``Orchestrator.run_query``, not its public surface) so the test
    is deterministic regardless of whether qmd is installed locally.
    """
    monkeypatch.setenv("LIES_WIKI_ROOT", str(sample_wiki.root))

    from lies.query import synthesizer as q_syn
    from lies.query.synthesizer import _QmdUnavailable

    def fake_dispatch(fn, layout, question, top_n):  # type: ignore[no-untyped-def]
        raise _QmdUnavailable("simulated: qmd unavailable")

    with mock.patch.object(q_syn, "_qmd_search_dispatch", new=fake_dispatch):
        result = query("anything")

    assert result.fallback_used is True
    assert result.fallback_reason == "qmd_unavailable"


# ---------------------------------------------------------------------------
# lint — deterministic health-check
# ---------------------------------------------------------------------------


def test_lint_returns_markdown_report(
    sample_wiki, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """lint delegates to Orchestrator.run_lint end-to-end.

    Mocks only the agent's ``run_sync`` (the linter sub-agent's LLM
    call) so the real Orchestrator runs. Asserts the deterministic
    host-side report was written to ``wiki/lint-report.md`` and that
    a parseable entry was appended to ``wiki/log.md``.
    """
    monkeypatch.setenv("LIES_WIKI_ROOT", str(sample_wiki.root))
    orch = Orchestrator(wiki_root=sample_wiki.root, model="test")

    def fake_run_sync(self, prompt: str):  # type: ignore[no-untyped-def]
        return mock.Mock(output="lint done")

    with mock.patch.object(type(orch._agent), "run_sync", new=fake_run_sync):
        out = lint()

    layout = sample_wiki
    # Artifact 1: lint-report.md exists and matches the returned string.
    assert layout.lint_report_path.exists()
    on_disk = layout.lint_report_path.read_text(encoding="utf-8")
    assert "Lint report" in on_disk
    assert on_disk == out

    # Artifact 2: log.md has a parseable lint entry.
    log_text = layout.log_path.read_text(encoding="utf-8")
    assert "lint" in log_text
    assert any(
        line.startswith("## [") and " lint " in line
        for line in log_text.splitlines()
    ), f"no parseable lint log entry; got:\n{log_text!r}"


# ---------------------------------------------------------------------------
# Server identity
# ---------------------------------------------------------------------------


def test_mcp_server_has_correct_name() -> None:
    """The FastMCP instance is named 'lies'."""
    assert mcp.name == "lies"