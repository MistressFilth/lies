"""Tests for the LIES MCP server resources, page template, and prompt."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from lies.mcp.server import (
    ask_wiki_answer,
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


def test_cite_prompt_documents_secondary_marker() -> None:
    """The cite prompt prose tells the LLM to prefix wiki-only hits with [secondary]."""
    out = cite(
        question="what is pydantic-ai?",
        tag_expr="python",
        exclude_tags=["chat"],
        top_k=3,
    )
    assert "secondary" in out.lower()


def test_answer_prompt_parses_c_prefix() -> None:
    """``+c:opencode <question>`` → ``tag_expr='c:opencode'``, no exclude.

    Regression for the live bug where the calling LLM hallucinated
    ``exclude_tags='does'`` from the question text. The slash prompt
    parses the include atom itself; the calling LLM has nothing to fill.
    """
    out = ask_wiki_answer(
        question="+c:opencode where does X keep settings?",
        name="default",
        collection="opencode",
    )
    assert isinstance(out, str)
    assert "tag_expr: 'c:opencode'" in out
    assert "exclude_tags: []" in out
    assert "question: where does X keep settings?" in out


def test_answer_prompt_parses_combined_atom_and_exclude() -> None:
    """``+c:opencode -draft what...`` → include + single exclude."""
    out = ask_wiki_answer(
        question="+c:opencode -draft what is the API?",
        name="default",
        collection="opencode",
    )
    assert "tag_expr: 'c:opencode'" in out
    assert "exclude_tags: ['draft']" in out
    assert "question: what is the API?" in out


def test_answer_prompt_parses_multi_atom_chain() -> None:
    """``+airflow&provider how do I...`` → AND chain, two atoms.

    ``_render_include`` preserves left-to-right operator order, so the
    rendered string is ``airflow&provider`` (not the reversed form).
    Uses unqualified atoms because ``+t:foo&c:bar`` in a single argv
    token trips a known ``_split_argv_token_for_ops`` limitation
    (``:`` is not a shlex wordchar). The CLI grammar splits those as
    separate argv tokens, but a single-token ``&`` chain is the
    common form per the spec.
    """
    out = ask_wiki_answer(
        question="+airflow&provider how do I configure?",
        name="default",
    )
    assert "tag_expr: 'airflow&provider'" in out
    assert "question: how do I configure?" in out


def test_answer_prompt_parses_quoted_exclude() -> None:
    """``+c:opencode -"airflow provider" ...`` → quoted exclude tag.

    shlex unquotes the ``"airflow provider"`` arg so the exclude tag
    arrives as a single multi-word string; the prompt renders the list
    with a single element.
    """
    out = ask_wiki_answer(
        question='+c:opencode -"airflow provider" what does it do?',
        name="default",
    )
    assert "tag_expr: 'c:opencode'" in out
    assert "exclude_tags: ['airflow provider']" in out


def test_answer_prompt_surfaces_parse_error() -> None:
    """``+a&`` (dangling operator) → parser error rendered verbatim.

    The slash prompt must NOT silently fall back to no-filter on a
    grammar error -- that would let the calling LLM retry with the
    hand-rewritten args. The fix surfaces the parser's message so the
    LLM tells the operator what went wrong.
    """
    out = ask_wiki_answer(
        question="+a&",
        name="default",
    )
    assert "Filter parse error" in out
    assert "dangling operator" in out


def test_answer_prompt_plain_question_unchanged() -> None:
    """Plain question (no filter) → no tag_expr, full text is the question.

    The pre-fix behaviour is preserved when there is no include/exclude
    prefix in the question argument.
    """
    out = ask_wiki_answer(
        question="Where does opencode keep settings?",
        name="default",
    )
    assert "tag_expr: None" in out
    assert "exclude_tags: []" in out
    assert "question: Where does opencode keep settings?" in out


def test_answer_prompt_no_false_positive_mid_question() -> None:
    """``c++ tutorial`` mid-question is NOT a filter.

    The parser only treats ``argv[0]`` as a chain start when it begins
    with ``+``. A bare ``c++ tutorial`` token in the middle of the
    question never matches the include/exclude rule.
    """
    out = ask_wiki_answer(
        question="how do I write a c++ tutorial?",
        name="default",
    )
    assert "tag_expr: None" in out
    assert "question: how do I write a c++ tutorial?" in out


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
