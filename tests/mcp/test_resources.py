"""Tests for the LIES MCP server resources, page template, and prompt."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from lies.mcp.server import (
    cite,
    init_wiki,
    wiki_index,
    wiki_lint_report,
    wiki_log,
    wiki_page,
    wiki_status,
)
from lies.memory.models import WikiPlanInvalid
from lies.wiki.wiki import Wiki


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
    """Materialise a wiki at the XDG DATA_HOME location with a fixture corpus.

    Copied from ``tests/fixtures/sample-wiki`` so the resource tests
    see the same page tree as the integration tests (``index.md``,
    ``entities/postgres.md``, etc.).
    """
    fixture_wiki = Path(__file__).parent.parent / "fixtures" / "sample-wiki"
    target = Wiki.data_root_for(wiki_name)
    shutil.copytree(fixture_wiki, target)
    Wiki.require(wiki_name)
    return Wiki.require(wiki_name)


def test_wiki_status_returns_qmd_and_log_tail(
    registered_wiki: Wiki,
    wiki_name: str,
) -> None:
    out = wiki_status(name=wiki_name)
    assert "=== qmd status ===" in out
    assert "=== last 10 log entries ===" in out


def test_wiki_index_returns_raw_markdown(
    registered_wiki: Wiki,
    wiki_name: str,
) -> None:
    out = wiki_index(name=wiki_name)
    assert "Index" in out  # fixture index has a heading


def test_wiki_log_returns_raw_markdown(
    registered_wiki: Wiki,
    wiki_name: str,
) -> None:
    out = wiki_log(name=wiki_name)
    assert isinstance(out, str)


def test_wiki_lint_report_missing_returns_empty_string(
    wiki_name: str,
) -> None:
    """When no lint has run yet, the resource returns '' (not 404)."""
    Wiki.data_root_for(wiki_name).mkdir(parents=True, exist_ok=True)
    out = wiki_lint_report(name=wiki_name)
    assert out == ""


def test_wiki_page_returns_file_contents(
    registered_wiki: Wiki,
    wiki_name: str,
) -> None:
    out = wiki_page("index.md", name=wiki_name)
    assert "Index" in out


def test_wiki_page_rejects_traversal(
    registered_wiki: Wiki,
    wiki_name: str,
) -> None:
    """``..`` traversal out of the wiki/ directory raises WikiPlanInvalid."""
    with pytest.raises(WikiPlanInvalid, match=r"\.\."):
        wiki_page("../../etc/passwd", name=wiki_name)


def test_wiki_page_rejects_absolute(
    registered_wiki: Wiki,
    wiki_name: str,
) -> None:
    """An absolute path is rejected (must be relative)."""
    with pytest.raises(WikiPlanInvalid, match="relative"):
        wiki_page("/etc/passwd", name=wiki_name)


def test_wiki_page_returns_empty_for_missing_file(
    registered_wiki: Wiki,
    wiki_name: str,
) -> None:
    """A page path that resolves cleanly under wiki/ but doesn't exist returns ''."""
    out = wiki_page("entities/does-not-exist.md", name=wiki_name)
    assert out == ""


def test_cite_prompt_routes_to_ground() -> None:
    """The cite slash templates a `ground` tool call and the snippet render form.

    Pins the routing contract: the returned string must contain the
    routed tool name (`ground`) and the citation render marker (`[[`).
    Regression catches silent re-template to `answer` or rewrites to a
    non-routing prose form.
    """
    out = cite(
        question="what is pydantic-ai?",
        tag_expr="python",
        exclude_tags=["chat"],
        top_k=3,
    )
    assert isinstance(out, str)
    assert "ground" in out
    assert "[[" in out
    assert "ArchivistCoverageError" in out


def test_init_wiki_round_trips_with_resources(wiki_name: str) -> None:
    """Resources can read a wiki that ``init_wiki`` just created.

    The resource handlers resolve via ``resolve_wiki(name)``; the
    init_wiki tool creates the XDG dirs + writes the schema. Together
    they prove the new tool is on the same resolution path as the
    resources.
    """
    info = init_wiki(wiki_name)
    assert info["name"] == wiki_name
    # The wiki is now registered; the status resource read succeeds.
    out = wiki_status(name=wiki_name)
    assert "=== qmd status ===" in out
