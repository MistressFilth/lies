"""Tests for the LIES MCP server resources, page template, and prompt."""
from __future__ import annotations

import pytest

from lies.mcp.resolution import WikiRootError
from lies.mcp.server import (
    _wiki_index_impl as wiki_index,
)
from lies.mcp.server import (
    _wiki_lint_report_impl as wiki_lint_report,
)
from lies.mcp.server import (
    _wiki_log_impl as wiki_log,
)
from lies.mcp.server import (
    _wiki_page_impl as wiki_page,
)
from lies.mcp.server import (
    _wiki_status_impl as wiki_status,
)
from lies.mcp.server import (
    ask_wiki,
)


def test_wiki_status_returns_qmd_and_log_tail(
    sample_wiki, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("LIES_WIKI_ROOT", str(sample_wiki.root))
    # qmd is likely absent in CI; the handler must catch and embed the error.
    out = wiki_status()
    assert "=== qmd status ===" in out
    assert "=== last 10 log entries ===" in out


def test_wiki_index_returns_raw_markdown(sample_wiki, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIES_WIKI_ROOT", str(sample_wiki.root))
    out = wiki_index()
    assert "# Index" in out or "Index" in out  # fixture index has a heading


def test_wiki_log_returns_raw_markdown(sample_wiki, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIES_WIKI_ROOT", str(sample_wiki.root))
    out = wiki_log()
    assert isinstance(out, str)


def test_wiki_lint_report_missing_returns_empty_string(sample_wiki, monkeypatch: pytest.MonkeyPatch) -> None:
    """When no lint has run yet, the resource returns '' (not 404)."""
    monkeypatch.setenv("LIES_WIKI_ROOT", str(sample_wiki.root))
    # Remove the report if it exists from a prior test.
    report_path = sample_wiki.lint_report_path
    if report_path.exists():
        report_path.unlink()
    out = wiki_lint_report()
    assert out == ""


def test_wiki_page_returns_file_contents(sample_wiki, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIES_WIKI_ROOT", str(sample_wiki.root))
    out = wiki_page("index.md")
    assert "Index" in out


def test_wiki_page_rejects_traversal(sample_wiki, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIES_WIKI_ROOT", str(sample_wiki.root))
    with pytest.raises(WikiRootError):
        wiki_page("../../etc/passwd")


def test_wiki_page_rejects_absolute(sample_wiki, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIES_WIKI_ROOT", str(sample_wiki.root))
    with pytest.raises(WikiRootError):
        wiki_page("/etc/passwd")


def test_wiki_page_returns_empty_for_missing_file(sample_wiki, monkeypatch: pytest.MonkeyPatch) -> None:
    """A page path that resolves cleanly under wiki/ but doesn't exist returns ''."""
    monkeypatch.setenv("LIES_WIKI_ROOT", str(sample_wiki.root))
    out = wiki_page("entities/does-not-exist.md")
    assert out == ""


def test_ask_wiki_prompt_includes_question() -> None:
    out = ask_wiki("What is MVCC?")
    assert "What is MVCC?" in out
    # The prompt mentions the tool name so the LLM uses it.
    assert "query" in out.lower()
