"""Tests for the LIES MCP server tools.

Each tool is tested by calling the decorated function directly after
registering the server module. The Orchestrator's agent ``run_sync``
is mocked so no real LLM call is made — same pattern as
``tests/integration/test_end_to_end.py``. The real
``Orchestrator.run_ingest`` and ``Orchestrator.run_lint`` methods are
NOT mocked, so the tool-to-orchestrator delegation is exercised
end-to-end. ``Orchestrator.run_query`` is mocked because the underlying
synthesizer still expects a ``WikiLayout`` (the Wiki→synthesizer
adapter is Task 17's work).

The XDG-role redirect fixture ``_redirect_xdg`` sets up a hermetic
``XDG_*_HOME`` per test so ``Wiki.data_root_for(name)`` lands under
``tmp_path`` and ``resolve_wiki(name)`` succeeds when the test (or
the tool) creates the wiki there.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

import pytest

from lies import __version__, xdg
from lies.agents.linter import LintReport
from lies.errors import WikiAlreadyExists
from lies.mcp.server import (
    SynthesizedMcpAnswer,
    ingest_source,
    init_wiki,
    lint,
    mcp,
    query,
)
from lies.orchestrator import Orchestrator
from lies.query.models import SynthesizedAnswer
from lies.schema.loader import load_default_schema
from lies.wiki.wiki import Wiki
from tests.conftest import models_for_tests


@pytest.fixture(autouse=True)
def _use_test_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default every agent's model env override to ``"test"`` so the
    orchestrator can build without a real provider key.

    The orchestrator now resolves per-agent models from
    ``LIES_<AGENT>_MODEL`` env overrides (one var per ``AGENT_ROSTER``
    entry), so this fixture sets every roster entry's override to the
    placeholder ``"test"`` string. Per-tool tests that exercise real
    Orchestrator behavior pass ``models_for_tests("test")`` explicitly
    so the fixture's env default is overridden in those paths.
    """
    from lies.providers import AGENT_ROSTER

    for name in AGENT_ROSTER:
        monkeypatch.setenv(f"LIES_{name.upper()}_MODEL", "test")


@pytest.fixture(autouse=True)
def _redirect_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Pin XDG role roots under ``tmp_path`` so wiki paths are hermetic."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("LIES_WIKI_NAME", raising=False)


@pytest.fixture
def wiki_name() -> str:
    """A wiki name to use across tests that need a registered wiki."""
    return "test"


@pytest.fixture
def registered_wiki(wiki_name: str) -> Wiki:
    """Materialise the five XDG role dirs for ``wiki_name`` so ``resolve_wiki`` works.

    Creates the data_root dir (the only one ``Wiki.require`` checks)
    plus the wiki/raw and wiki/wiki subdirs under it. Returns the
    resolved :class:`Wiki` so callers can read paths/structure.
    """
    wiki = Wiki(
        name=wiki_name,
        data_root=Wiki.data_root_for(wiki_name),
        config_root=xdg.config_home() / "lies" / wiki_name,
        cache_root=xdg.cache_home() / "lies" / wiki_name,
        state_root=xdg.state_home() / "lies" / wiki_name,
        runtime_root=xdg.runtime_dir_for(wiki_name),
    )
    wiki.data_root.mkdir(parents=True, exist_ok=True)
    wiki.raw_dir.mkdir(parents=True, exist_ok=True)
    wiki.wiki_dir.mkdir(parents=True, exist_ok=True)
    return wiki


# ---------------------------------------------------------------------------
# Helpers — same shape as ``tests/integration/test_end_to_end.py``
# ---------------------------------------------------------------------------


def _log_lines(repo: Path) -> list[str]:
    """Return ``[<sha> <subject>, ...]`` newest-first."""
    result = subprocess.run(
        ["git", "log", "--pretty=%H %s"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.splitlines()


# ---------------------------------------------------------------------------
# init_wiki — bootstrap a new wiki under XDG
# ---------------------------------------------------------------------------


def test_init_wiki_creates_wiki_structure(tmp_path: Path, wiki_name: str) -> None:
    info = init_wiki(wiki_name)
    assert info["name"] == wiki_name
    assert info["version"] == f"lies {__version__}" or info["version"] == __version__
    wiki = Wiki.require(wiki_name)
    # All five XDG role dirs created.
    assert wiki.data_root.is_dir()
    assert wiki.config_root.is_dir()
    assert wiki.cache_root.is_dir()
    assert wiki.state_root.is_dir()
    assert wiki.runtime_root.is_dir()
    # data_root has raw/ and wiki/ subdirs.
    assert wiki.raw_dir.is_dir()
    assert wiki.wiki_dir.is_dir()
    # Schema at the XDG config_root, not the data_root.
    assert wiki.schema_path.is_file()
    assert wiki.schema_path.read_text(encoding="utf-8") == load_default_schema()
    # Git is initialized in data_root and has at least the initial commit.
    assert (wiki.data_root / ".git").exists()
    log = _log_lines(wiki.data_root)
    assert log, "expected at least the initial commit"
    _sha, _, subject = log[0].partition(" ")
    assert subject.startswith("Initial") or subject == "initial wiki"


def test_init_wiki_rejects_duplicate(wiki_name: str) -> None:
    init_wiki(wiki_name)
    with pytest.raises(WikiAlreadyExists, match=wiki_name):
        init_wiki(wiki_name)


def test_init_wiki_rejects_invalid_name(tmp_path: Path) -> None:
    """Wiki name validation (no path separators, no leading dot)."""
    from lies.errors import WikiNameError

    with pytest.raises(WikiNameError):
        init_wiki("foo/bar")


# ---------------------------------------------------------------------------
# ingest_source — atomic ingest through the orchestrator
# ---------------------------------------------------------------------------


def test_ingest_source_returns_ingested_string(
    registered_wiki: Wiki,
    wiki_name: str,
) -> None:
    """ingest_source → Orchestrator.run_ingest → sync_collection.

    Mocks sync_collection so the real MCP → Orchestrator delegation is
    exercised. Asserts the MCP tool returns the wrapper's documented
    back-compat string.
    """
    with mock.patch("lies.etl.sync_helper.sync_collection") as m:
        out = ingest_source(
            "raw/articles/sample-article.md",
            name=wiki_name,
        )

    # MCP tool returns the wrapper's documented back-compat string.
    assert out == "ingested raw/articles/sample-article.md"
    # sync_collection was called with (wiki, source.stem, force=False).
    m.assert_called_once()
    args, kwargs = m.call_args
    assert args[0] == registered_wiki
    assert args[1] == "sample-article"
    assert kwargs == {"force": False}


# ---------------------------------------------------------------------------
# query — extractive answer with structured fallback metadata
# ---------------------------------------------------------------------------


def test_query_returns_synthesized_mcp_answer(
    registered_wiki: Wiki,
    wiki_name: str,
) -> None:
    """query returns a SynthesizedMcpAnswer with the right shape.

    Mocks ``Orchestrator.run_query`` (the synthesizer still expects a
    ``WikiLayout``; the Wiki→synthesizer adapter is Task 17's work) so
    the test exercises the actual MCP → Orchestrator delegation without
    the broken synthesizer path.
    """
    fake = SynthesizedAnswer(
        answer="### What is MVCC?\n\nA concurrency protocol.",
        fallback_used=False,
        fallback_reason="",
    )

    with mock.patch.object(Orchestrator, "run_query", return_value=fake):
        result = query("What is MVCC?", name=wiki_name)

    assert isinstance(result, SynthesizedMcpAnswer)
    assert result.answer == "### What is MVCC?\n\nA concurrency protocol."
    assert result.fallback_used is False
    assert result.fallback_reason is None


def test_query_maps_empty_fallback_reason_to_none(
    registered_wiki: Wiki,
    wiki_name: str,
) -> None:
    """When qmd serves the query, ``fallback_reason`` becomes ``None``."""
    fake = SynthesizedAnswer(
        answer="X",
        fallback_used=False,
        fallback_reason="",
    )

    with mock.patch.object(Orchestrator, "run_query", return_value=fake):
        result = query("What is MVCC?", name=wiki_name)

    assert isinstance(result, SynthesizedMcpAnswer)
    assert result.fallback_used is False
    assert result.fallback_reason is None


def test_query_propagates_fallback_reason(
    registered_wiki: Wiki,
    wiki_name: str,
) -> None:
    """When qmd is unavailable, fallback_used and fallback_reason surface."""
    fake = SynthesizedAnswer(
        answer="X",
        fallback_used=True,
        fallback_reason="qmd_unavailable",
    )

    with mock.patch.object(Orchestrator, "run_query", return_value=fake):
        result = query("anything", name=wiki_name)

    assert result.fallback_used is True
    assert result.fallback_reason == "qmd_unavailable"


# ---------------------------------------------------------------------------
# lint — deterministic health-check
# ---------------------------------------------------------------------------


def test_lint_returns_markdown_report(
    registered_wiki: Wiki,
    wiki_name: str,
) -> None:
    """lint delegates to Orchestrator.run_lint end-to-end.

    Mocks only the agent's ``run_sync`` (the linter sub-agent's LLM
    call) so the real Orchestrator runs. Asserts the deterministic
    host-side report was written to ``wiki/lint-report.md`` and that
    a parseable entry was appended to ``wiki/log.md``.
    """
    orch = Orchestrator(wiki=registered_wiki, models=models_for_tests("test"))

    def fake_run_sync(self, prompt: str):  # type: ignore[no-untyped-def]
        return mock.Mock(output="lint done")

    with (
        mock.patch.object(type(orch._agent), "run_sync", new=fake_run_sync),
        mock.patch.object(
            Orchestrator,
            "_call_linter",
            return_value=(LintReport(findings=[], report_markdown=""), None),
        ),
    ):
        out = lint(name=wiki_name)

    # Artifact 1: lint-report.md exists and matches the returned string.
    report_path = registered_wiki.wiki_dir / "lint-report.md"
    assert report_path.exists()
    on_disk = report_path.read_text(encoding="utf-8")
    assert "Lint report" in on_disk
    assert on_disk == out

    # Artifact 2: log.md has a parseable lint entry.
    log_text = (registered_wiki.wiki_dir / "log.md").read_text(encoding="utf-8")
    assert "lint" in log_text
    assert any(line.startswith("## [") and " lint " in line for line in log_text.splitlines()), (
        f"no parseable lint log entry; got:\n{log_text!r}"
    )


# ---------------------------------------------------------------------------
# Server identity
# ---------------------------------------------------------------------------


def test_mcp_server_has_correct_name() -> None:
    """The FastMCP instance is named 'lies'."""
    assert mcp.name == "lies"
