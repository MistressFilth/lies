"""Tests for the LIES MCP server resources, page template, and prompt."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from lies.mcp.server import (
    _wiki_catalog_impl,
    _wiki_index_impl,
    _wiki_lint_report_impl,
    ask_question,
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


def test_wiki_index_library_mode_returns_envelope(
    wiki_name: str,
) -> None:
    """No wiki registered → ``wiki_index`` returns ``{"mode": "library"}``.

    Pins the regression for the live bug where
    ``ReadMcpResourceTool(uri='wiki://index')`` returned ``""`` and the
    LLM caller had no signal to route through the library path. The
    envelope gives the LLM a stable JSON shape (``mode: 'library'``)
    to dispatch on without parsing the wiki catalog first.
    """
    from lies.errors import WikiNotRegistered
    from lies.mcp.resolution import resolve_wiki

    with pytest.raises(WikiNotRegistered):
        resolve_wiki(name=wiki_name)

    out = _wiki_index_impl(name=wiki_name)
    parsed = json.loads(out)
    assert parsed == {"mode": "library"}


def test_wiki_lint_report_library_mode_returns_envelope(
    wiki_name: str,
) -> None:
    """No wiki registered → ``wiki_lint_report`` returns ``{"mode": "library", "status": "no_wiki"}``.

    The ``status: "no_wiki"`` field is concrete — a wiki never existed,
    so a lint report is meaningless. Mirrors the
    ``test_wiki_index_library_mode_returns_envelope`` contract and
    pins the regression for the live bug where
    ``ReadMcpResourceTool(uri='wiki://lint-report')`` returned ``""``.
    """
    from lies.errors import WikiNotRegistered
    from lies.mcp.resolution import resolve_wiki

    with pytest.raises(WikiNotRegistered):
        resolve_wiki(name=wiki_name)

    out = _wiki_lint_report_impl(name=wiki_name)
    parsed = json.loads(out)
    assert parsed == {"mode": "library", "status": "no_wiki"}


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


def test_wiki_catalog_library_mode_no_wiki_registered(
    wiki_name: str,
) -> None:
    """No wiki registered under the redirected XDG roots.

    ``resolve_wiki`` raises ``WikiNotRegistered`` so the resource
    falls through to library mode. The envelope reports ``mode:
    "library"`` with an empty ``collections`` list (the test env has
    no library either, so the name set is empty). Pins the
    regression for the live bug where the MCP ``wiki://catalog``
    resource returned library synthesis slugs instead of an
    informative envelope.
    """
    from lies.errors import WikiNotRegistered

    with pytest.raises(WikiNotRegistered):
        # Sanity-check the precondition: without a registered wiki,
        # resolve_wiki does raise. The fix is that the resource
        # surfaces this condition via a stable JSON envelope rather
        # than propagating the exception.
        from lies.mcp.resolution import resolve_wiki

        resolve_wiki(name=wiki_name)

    out = _wiki_catalog_impl(name=wiki_name)
    parsed = json.loads(out)
    assert parsed == {"mode": "library", "collections": []}


def test_wiki_catalog_library_mode_lists_registered_collections(
    wiki_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Library-mode envelope carries every registered collection name.

    Stubs :func:`lies.library.registry.library_collection_names` so
    the assertion sees a deterministic non-empty collection set
    regardless of the on-disk library state. Pins the contract that
    the resource serialises the full set (sorted) — not a subset,
    not a hand-rolled tag list.
    """
    monkeypatch.setattr(
        "lies.library.registry.library_collection_names",
        lambda: frozenset({"alpha", "beta"}),
    )
    out = _wiki_catalog_impl(name=wiki_name)
    parsed = json.loads(out)
    assert parsed == {"mode": "library", "collections": ["alpha", "beta"]}


def test_wiki_catalog_wiki_mode_returns_catalog_page_list(
    registered_wiki: Wiki,
    wiki_name: str,
) -> None:
    """Wiki-mode returns the existing ``list[CatalogPage]`` JSON array.

    The fixture wiki has multiple ``.md`` files under ``wiki/``;
    the resource opens ``<wiki>/.lies/catalog.db`` and dumps every
    row. The shape is a JSON array of ``CatalogPage.model_dump()``
    dicts — not the library-mode envelope. This regression pins the
    branch unchanged so the library-mode fix does not perturb the
    wiki-mode contract.
    """
    out = _wiki_catalog_impl(name=wiki_name)
    parsed = json.loads(out)
    assert isinstance(parsed, list)
    assert len(parsed) > 0
    for page in parsed:
        # Every row is a CatalogPage-shaped dict.
        assert "slug" in page
        assert "title" in page
        assert "section" in page


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
        text="+c:opencode where does X keep settings?",
    )
    assert isinstance(out, str)
    assert "tag_expr: 'c:opencode'" in out
    assert "exclude_tags: []" in out
    assert "question: where does X keep settings?" in out


def test_answer_prompt_parses_combined_atom_and_exclude() -> None:
    """``+c:opencode -draft what...`` → include + single exclude."""
    out = ask_wiki_answer(
        text="+c:opencode -draft what is the API?",
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
        text="+airflow&provider how do I configure?",
    )
    assert "tag_expr: 'airflow&provider'" in out
    assert "question: how do I configure?" in out


def test_answer_prompt_parses_quoted_exclude() -> None:
    """``+c:opencode -"airflow provider" ...`` → quoted exclude tag.

    shlex unquotes the ``"airflow provider"`` arg so the exclude tag
    arrives as a single multi-word string; the prompt renders the list
    with a single element. Task 3 / f15-exclude-compound changed the
    exclude representation from a flat string to an ``Include`` / ``And``
    / ``Or`` AST, so :func:`_render_include` quotes the multi-word tag
    (``"airflow provider"``) so it round-trips through :func:`parse`
    intact — the rendered exclude entry is ``'\\"airflow provider\\"'``.
    The LLM reads the rendered string verbatim and forwards it to the
    ``answer`` tool's ``exclude_tags=`` kwarg, where :func:`parse` strips
    the outer quotes back to the multi-word atom.
    """
    out = ask_wiki_answer(
        text='+c:opencode -"airflow provider" what does it do?',
    )
    assert "tag_expr: 'c:opencode'" in out
    assert "exclude_tags: ['\"airflow provider\"']" in out


def test_answer_prompt_surfaces_parse_error() -> None:
    """``+a&`` (dangling operator) → parser error rendered verbatim.

    The slash prompt must NOT silently fall back to no-filter on a
    grammar error -- that would let the calling LLM retry with the
    hand-rewritten args. The fix surfaces the parser's message so the
    LLM tells the operator what went wrong.
    """
    out = ask_wiki_answer(
        text="+a&",
    )
    assert "Filter parse error" in out
    assert "dangling operator" in out


def test_answer_prompt_plain_question_unchanged() -> None:
    """Plain question (no filter) → no tag_expr, full text is the question.

    The pre-fix behaviour is preserved when there is no include/exclude
    prefix in the question argument.
    """
    out = ask_wiki_answer(
        text="Where does opencode keep settings?",
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
        text="how do I write a c++ tutorial?",
    )
    assert "tag_expr: None" in out
    assert "question: how do I write a c++ tutorial?" in out


def test_answer_prompt_handles_multi_word_input() -> None:
    """Full multi-word input parses correctly (slash dispatcher regression).

    Regression for the dispatch bug: when the slash prompt had multiple
    positional args, Claude Code's dispatcher tokenized the input on
    whitespace and assigned each token to a separate arg, dropping
    everything past the third token. The fix collapses to a single
    ``text`` positional arg so the dispatcher passes the entire
    remainder of the line verbatim. The full question text must then
    round-trip through the filter parser.
    """
    out = ask_wiki_answer(
        text="+c:opencode Where does opencode keep its settings on Linux?",
    )
    assert "tag_expr: 'c:opencode'" in out
    assert "exclude_tags: []" in out
    assert "question: Where does opencode keep its settings on Linux?" in out


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


def test_ask_question_parses_filter() -> None:
    """``+c:opencode <question>`` → ``tag_expr='c:opencode'``, no exclude.

    Regression for the slash-tokenization bug: Claude Code's dispatcher
    tokenizes the slash command on whitespace BEFORE invoking the MCP
    prompt function, dropping everything past the first token. The
    ``ask_question`` tool bypasses that path because Claude Code passes
    structured JSON args to tools verbatim, so multi-word strings
    round-trip intact. The LLM calls ``ask_question`` first, then
    forwards the returned kwargs to the ``answer`` tool.
    """
    out = ask_question(text="+c:opencode Where does opencode keep settings?")
    assert out["question"] == "Where does opencode keep settings?"
    assert out["tag_expr"] == "c:opencode"
    assert out["exclude_tags"] == []


def test_ask_question_no_filter() -> None:
    """Plain question (no filter prefix) → ``tag_expr=None``."""
    out = ask_question(text="Where does opencode keep settings?")
    assert out["question"] == "Where does opencode keep settings?"
    assert out["tag_expr"] is None
    assert out["exclude_tags"] == []


def test_ask_question_parse_error() -> None:
    """``+a&`` (dangling operator) → structured envelope with error key.

    The tool never raises on parser errors — the LLM needs a
    machine-readable shape it can inspect, not an exception trace.
    """
    out = ask_question(text="+a&")
    assert "error" in out
    assert "dangling operator" in out["error"]


def test_ask_question_compound_exclude_and() -> None:
    """``-c:foo&c:bar <question>`` → ``exclude_tags=['c:foo&c:bar']``.

    The exclude path mirrors the include path: ``&`` chains atoms
    into an ``And`` AST that ``_render_include`` flattens back to
    a single ``c:foo&c:bar`` string. The downstream ``query`` /
    ``answer`` boundary re-parses that string, so round-tripping
    through the MCP wire format must preserve the operator.
    """
    out = ask_question(text="-c:foo&c:bar what is X?")
    assert out["question"] == "what is X?"
    assert out["tag_expr"] is None
    assert out["exclude_tags"] == ["c:foo&c:bar"]


def test_ask_question_compound_exclude_or() -> None:
    """``-c:foo|c:bar <question>`` → ``exclude_tags=['c:foo|c:bar']``.

    Same as the AND case but with ``|``: the parser produces an
    ``Or`` AST and ``_render_include`` emits the ``|`` operator
    between the two atoms.
    """
    out = ask_question(text="-c:foo|c:bar what is X?")
    assert out["question"] == "what is X?"
    assert out["tag_expr"] is None
    assert out["exclude_tags"] == ["c:foo|c:bar"]


def test_ask_question_empty() -> None:
    """Empty input → structured envelope with error key, no exception."""
    out = ask_question(text="")
    assert "error" in out
