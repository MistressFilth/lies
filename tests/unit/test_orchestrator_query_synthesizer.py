"""Unit tests for Orchestrator._call_query_synthesizer + run_query wiring."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

import pytest

from lies.agents.query_synthesizer import QueryAnswer
from lies.orchestrator import Orchestrator
from lies.query.synthesizer import PageRead, qmd_query, set_qmd_search
from tests.conftest import make_wiki, models_for_tests


@pytest.fixture
def orch(tmp_path: Path) -> Orchestrator:
    root = tmp_path / "wiki"
    (root / "wiki" / "concepts").mkdir(parents=True)
    (root / "raw").mkdir(parents=True)
    (root / "wiki" / "concepts" / "alpha.md").write_text(
        "---\ntitle: Alpha\n---\n\nAlpha is the first letter.\n", encoding="utf-8"
    )
    (root / "wiki" / "index.md").write_text(
        "# Index\n\n## concepts\n\n- [Alpha](concepts/alpha.md) — `alpha`\n",
        encoding="utf-8",
    )
    wiki = make_wiki(name="qsynth", data_root=root)
    wiki.config_root.mkdir(parents=True, exist_ok=True)
    (wiki.config_root / "schema.md").write_text("## Page types\n- concept\n", encoding="utf-8")
    subprocess.run(["git", "init", "--initial-branch=main", str(root)], check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True)
    return Orchestrator(wiki=wiki, models=models_for_tests("test"))


@pytest.fixture(autouse=True)
def _no_real_qmd() -> None:
    """Never shell out to the real qmd binary from these tests.

    Uses ``set_qmd_search`` (the indirection seam in
    ``lies.query.synthesizer``) rather than ``monkeypatch.setattr`` on
    the module attribute: a captured default argument would otherwise
    shadow the rebind, and every test would shell out to the real qmd
    binary (~40s/test on systems where qmd is on PATH).
    """
    set_qmd_search(lambda *a, **kw: [{"path": "concepts/alpha.md", "score": 0.9}])
    yield
    set_qmd_search(qmd_query)


def _answer(**kwargs: object) -> QueryAnswer:
    base = {
        "answer": "Alpha is the first letter. [Alpha](wiki/concepts/alpha.md)",
        "citations": ["wiki/concepts/alpha.md"],
        "should_file": False,
    }
    base.update(kwargs)
    return QueryAnswer(**base)  # type: ignore[arg-type]


def test_orchestrator_has_query_synthesizer_agent(orch: Orchestrator) -> None:
    assert hasattr(orch, "_query_synthesizer_agent")
    assert orch._query_synthesizer_agent is not None


def test_run_query_uses_agent_answer_on_success(orch: Orchestrator) -> None:
    with mock.patch.object(
        type(orch._query_synthesizer_agent),
        "run_sync",
        return_value=mock.Mock(output=_answer(should_file=True)),
    ):
        # ``file=False`` opts out of the F3 file-back path so this
        # test stays focused on pure synthesis-success. The
        # ``should_file=True`` flag from the agent is still preserved
        # on the answer; only the file-back wiring is suppressed.
        result = orch.run_query("what is alpha?", file=False)

    assert result.synthesis_used is True
    assert result.synthesis_reason == ""
    assert result.answer == "Alpha is the first letter. [Alpha](wiki/concepts/alpha.md)"
    # citations are now ``list[Citation]`` (Task 6); the wiki pass
    # resolves to source="wiki" for this fixture.
    from lies.query.citation import Citation

    assert result.citations == [Citation(path="wiki/concepts/alpha.md", source="wiki")]
    assert result.should_file is True
    assert result.file_receipt is None


def test_run_query_falls_back_to_extractive_when_agent_raises(orch: Orchestrator) -> None:
    with mock.patch.object(
        type(orch._query_synthesizer_agent),
        "run_sync",
        side_effect=RuntimeError("model exploded"),
    ):
        result = orch.run_query("what is alpha?")

    assert result.synthesis_used is False
    assert result.synthesis_reason == "RuntimeError: model exploded"
    # Two-pass retrieval surfaces the same wiki hit once per pass
    # (library + wiki) — both resolve to the same wiki page; the retriever
    # dedupes on ``(rel_path, source)`` so the extractive body reports
    # one page, not two (Important 5).
    assert "Based on 1 wiki page(s)" in result.answer
    from lies.query.citation import Citation

    assert result.citations == [Citation(path="wiki/concepts/alpha.md", source="wiki")]


def test_run_query_drops_citations_the_agent_never_received(orch: Orchestrator) -> None:
    hallucinated = _answer(citations=["wiki/concepts/alpha.md", "wiki/concepts/ghost.md"])
    with mock.patch.object(
        type(orch._query_synthesizer_agent),
        "run_sync",
        return_value=mock.Mock(output=hallucinated),
    ):
        result = orch.run_query("what is alpha?")

    assert result.synthesis_used is True
    from lies.query.citation import Citation

    assert result.citations == [Citation(path="wiki/concepts/alpha.md", source="wiki")]
    assert "wiki/concepts/ghost.md" in result.synthesis_reason
    assert result.synthesis_reason.startswith("dropped 1 unretrieved citation(s)")


def test_run_query_skips_the_agent_when_no_pages_were_retrieved(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("lies.orchestrator.retrieve_pages", lambda *a, **kw: ([], "qmd_no_results"))
    with mock.patch.object(type(orch._query_synthesizer_agent), "run_sync") as run_sync:
        result = orch.run_query("what is alpha?")

    assert run_sync.call_count == 0
    assert result.synthesis_used is False
    assert result.synthesis_reason == "no pages retrieved"
    assert result.fallback_used is True


def test_run_query_calls_retrieve_pages_at_most_once_per_branch(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`retrieve_pages` must run once per `run_query` regardless of branch.

    The agent-success path uses `pages` directly; the agent-failure and
    empty-pages paths must build the extractive answer from the pages
    `retrieve_pages` already returned, not re-run it. Counts every
    branch.
    """
    call_count = 0

    def counting_retrieve(*_a: object, **_kw: object) -> tuple[list[object], str]:
        nonlocal call_count
        call_count += 1
        return ([], "qmd_no_results")

    monkeypatch.setattr("lies.orchestrator.retrieve_pages", counting_retrieve)

    # 1. Empty-pages branch: `retrieve_pages` once, agent never invoked.
    with mock.patch.object(type(orch._query_synthesizer_agent), "run_sync") as run_sync:
        orch.run_query("what is alpha?")
    assert call_count == 1
    assert run_sync.call_count == 0

    # 2. Agent-failure branch: `retrieve_pages` once, extractive builds
    #    from the cached pages, no second retrieval.
    pages = [
        PageRead(
            rel_path="wiki/concepts/alpha.md",
            title="Alpha",
            spans=[],
            source="wiki",
        ),
    ]

    def fake_retrieve_with_pages(*_a: object, **_kw: object) -> tuple[list[PageRead], str]:
        nonlocal call_count
        call_count += 1
        return pages, ""

    monkeypatch.setattr("lies.orchestrator.retrieve_pages", fake_retrieve_with_pages)
    call_count = 0

    with mock.patch.object(
        type(orch._query_synthesizer_agent),
        "run_sync",
        side_effect=RuntimeError("model exploded"),
    ):
        result = orch.run_query("what is alpha?")
    assert call_count == 1
    assert result.synthesis_used is False
    assert result.synthesis_reason == "RuntimeError: model exploded"


def test_call_query_synthesizer_runs_with_in_memory_spans_only(
    orch: Orchestrator,
) -> None:
    """F19 (Task 5): ``_call_query_synthesizer`` no longer reads files —
    the defensive read loop is gone. ``PageRead.spans`` already carries
    the parsed body from retrieval, and the synthesizer's
    ``QueryDeps.page_texts`` is derived from ``LibrarianOutput.excerpts``.

    Regression pin: a missing-on-disk page that arrived with empty
    spans must NOT crash the synthesis path. The synthesizer still
    runs (the agent receives an empty-spans deps) and the
    ``page_texts`` / ``page_sources`` derived properties reflect
    the in-memory spans verbatim.
    """

    captured: dict[str, object] = {}

    def capture(_self: object, _prompt: str, **kwargs: object) -> mock.Mock:
        captured["deps"] = kwargs["deps"]
        return mock.Mock(output=_answer())

    with mock.patch.object(type(orch._query_synthesizer_agent), "run_sync", capture):
        output, reason = orch._call_query_synthesizer(
            "what is alpha?",
            [
                PageRead(
                    rel_path="wiki/concepts/alpha.md",
                    title="Alpha",
                    spans=[],
                    source="wiki",
                ),
            ],
        )

    # Agent ran, returned its answer, no failure surfaced.
    assert output is not None
    assert reason == ""
    # Empty-spans page yields a derived empty-string body, not a
    # missing entry — the old behavior was to skip on read failure,
    # which doesn't apply post-F19 since the synthesizer doesn't
    # touch disk.
    deps = captured["deps"]
    assert deps.page_texts == {"wiki/concepts/alpha.md": ""}  # type: ignore[attr-defined]
    assert deps.page_sources == {"wiki/concepts/alpha.md": "wiki"}  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Critical 1 + Critical 4: path resolver branches on ``source`` and the
# deps carry a per-path source discriminator.
# ---------------------------------------------------------------------------


def test_call_query_synthesizer_threads_library_pages_through_excerpts(
    orch: Orchestrator,
) -> None:
    """F19 (Task 5): a library-sourced page carries ``source="library"``
    through to ``PageExcerpt.collection`` and onto
    ``QueryDeps.page_sources`` — the synthesizer sees the discriminator
    without re-reading the file from ``Library.collections_root``.

    The pre-F19 implementation read the file at this seam to verify
    the resolver landed at the library root; that contract is gone —
    retrieval populates ``PageRead.spans`` and the synthesizer just
    forwards them.
    """
    from lies.markdown_spans import Span

    rel_path = "claude_platform/skills.md"
    spans = [
        Span(heading_path=["Skills"], body="Library body.", code_fence=False, start_line=3),
    ]
    captured: dict[str, object] = {}

    def capture(_self: object, _prompt: str, **kwargs: object) -> mock.Mock:
        captured["deps"] = kwargs["deps"]
        return mock.Mock(output=_answer(citations=[rel_path]))

    with mock.patch.object(type(orch._query_synthesizer_agent), "run_sync", capture):
        output, reason = orch._call_query_synthesizer(
            "anything",
            [
                PageRead(
                    rel_path=rel_path,
                    title="Skills",
                    spans=spans,
                    source="library",
                ),
            ],
        )
    assert output is not None
    assert reason == ""
    deps = captured["deps"]
    # Library-sourced page flows through to the wiki/library discriminator.
    assert deps.page_sources[rel_path] == "library"  # type: ignore[attr-defined]
    # page_texts is derived from span bodies (code-fence excluded).
    assert deps.page_texts[rel_path] == "Library body."  # type: ignore[attr-defined]


def test_call_query_synthesizer_threads_wiki_pages_through_excerpts(
    orch: Orchestrator,
) -> None:
    """F19 (Task 5): a wiki-sourced page carries ``source="wiki"``
    through to ``QueryDeps.page_sources``. ``page_texts`` is derived
    from the in-memory ``PageRead.spans`` list (no disk read).
    """
    from lies.markdown_spans import Span

    captured: dict[str, object] = {}

    def capture(_self: object, _prompt: str, **kwargs: object) -> mock.Mock:
        captured["deps"] = kwargs["deps"]
        return mock.Mock(output=_answer())

    with mock.patch.object(type(orch._query_synthesizer_agent), "run_sync", capture):
        output, reason = orch._call_query_synthesizer(
            "what is alpha?",
            [
                PageRead(
                    rel_path="wiki/concepts/alpha.md",
                    title="Alpha",
                    spans=[
                        Span(
                            heading_path=[],
                            body="---\ntitle: Alpha\n---\n\nAlpha is the first letter.\n",
                            code_fence=False,
                            start_line=1,
                        ),
                    ],
                    source="wiki",
                ),
            ],
        )

    assert output is not None
    assert reason == ""
    deps = captured["deps"]
    assert deps.page_sources["wiki/concepts/alpha.md"] == "wiki"  # type: ignore[attr-defined]
    assert deps.page_texts["wiki/concepts/alpha.md"] == (  # type: ignore[attr-defined]
        "---\ntitle: Alpha\n---\n\nAlpha is the first letter.\n"
    )


def test_call_query_synthesizer_passes_through_empty_span_library_pages(
    orch: Orchestrator,
) -> None:
    """F19 (Task 5): a library-sourced page with empty spans flows
    through the synthesizer with an empty-string body — the
    defensive read loop is gone (no disk I/O here), and the agent
    still runs with the in-memory state the caller provided.

    Pre-F19 this test asserted the page was silently skipped on
    read failure; that semantic is now retrieval's responsibility
    (``parse_spans`` either succeeds or the page never reaches
    ``_call_query_synthesizer``).
    """
    from unittest import mock as _mock

    captured: dict[str, object] = {}

    def capture(_self: object, _prompt: str, **kwargs: object) -> _mock.Mock:
        captured["deps"] = kwargs["deps"]
        return _mock.Mock(output=_answer())

    with _mock.patch.object(type(orch._query_synthesizer_agent), "run_sync", capture):
        output, reason = orch._call_query_synthesizer(
            "anything",
            [
                PageRead(
                    rel_path="claude_platform/missing.md",
                    title="Missing",
                    spans=[],
                    source="library",
                ),
            ],
        )

    assert output is not None
    assert reason == ""
    deps = captured["deps"]
    # Empty-spans page yields empty body, not skipped.
    assert deps.page_texts == {"claude_platform/missing.md": ""}  # type: ignore[attr-defined]
    assert deps.page_sources == {"claude_platform/missing.md": "library"}  # type: ignore[attr-defined]


def test_call_query_synthesizer_populates_page_sources_for_each_excerpt(
    orch: Orchestrator,
) -> None:
    """F19 (Task 5): every ``PageRead`` arriving at the synthesizer
    carries through to ``QueryDeps.page_texts`` and
    ``QueryDeps.page_sources`` so the LLM prompt can render the
    discriminator for every page the agent sees."""
    captured: dict[str, object] = {}

    def capture(_self: object, _prompt: str, **kwargs: object) -> mock.Mock:
        captured["deps"] = kwargs["deps"]
        return mock.Mock(output=_answer())

    with mock.patch.object(type(orch._query_synthesizer_agent), "run_sync", capture):
        orch._call_query_synthesizer(
            "what is alpha?",
            [
                PageRead(
                    rel_path="wiki/concepts/alpha.md",
                    title="Alpha",
                    spans=[],
                    source="wiki",
                ),
            ],
        )

    deps = captured["deps"]
    # Both keys line up: every page_texts entry has a source entry.
    assert set(deps.page_texts.keys()) == set(deps.page_sources.keys())  # type: ignore[attr-defined]
    assert deps.page_sources["wiki/concepts/alpha.md"] == "wiki"  # type: ignore[attr-defined]


def test_set_qmd_search_rebinds_retrieve_pages_default(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`set_qmd_search` must rebind what `retrieve_pages` / `synthesize_answer`
    see when called without the ``qmd_search`` kwarg.

    Regression for the captured-default-argument leak: before the
    indirection seam, `monkeypatch.setattr` on the module attribute
    silently failed because `qmd_search=qmd_query` was evaluated at
    function-definition time. Each test then shelled out to the real
    qmd binary (~40s/test on systems where qmd is on PATH).

    After the two-pass refactor, ``retrieve_pages`` invokes the qmd
    callable twice (library pass + wiki-rooted pass); the indirection
    must reach both calls.
    """
    from lies.query import synthesize_answer
    from lies.query.synthesizer import retrieve_pages
    from lies.query.synthesizer import set_qmd_search as direct_set_qmd_search

    sentinel_calls: list[tuple[str, object, object, object]] = []

    def sentinel(
        _cwd: object, _question: object, _limit: object, **_kwargs: object
    ) -> list[dict[str, object]]:
        sentinel_calls.append(("sentinel", _cwd, _question, _limit))
        return [{"path": "concepts/alpha.md", "score": 0.9}]

    direct_set_qmd_search(sentinel)
    try:
        # Both functions honor the rebind when no kwarg is supplied.
        # ``retrieve_pages`` issues two qmd calls (library + wiki passes).
        retrieve_pages("what is alpha?", orch.wiki)
        assert len(sentinel_calls) == 2
        # ``synthesize_answer`` calls ``retrieve_pages`` once, which makes
        # two more qmd calls.
        synthesize_answer("what is alpha?", orch.wiki)
        assert len(sentinel_calls) == 4
    finally:
        # Restore the real binary so other tests aren't broken.
        direct_set_qmd_search(qmd_query)


def test_set_qmd_search_propagates_through_orchestrator(
    orch: Orchestrator,
) -> None:
    """`run_query` honors the indirection even though it doesn't pass
    the kwarg explicitly to ``retrieve_pages``.

    Two-pass retrieval invokes the qmd callable twice (library + wiki
    passes); both invocations must reach the rebind.
    """
    from lies.query.synthesizer import set_qmd_search as direct_set_qmd_search

    called: list[str] = []

    def fake(*_a: object, **_kw: object) -> list[dict[str, object]]:
        called.append("fake")
        return [{"path": "concepts/alpha.md", "score": 0.9}]

    direct_set_qmd_search(fake)
    try:
        with mock.patch.object(
            type(orch._query_synthesizer_agent),
            "run_sync",
            return_value=mock.Mock(output=_answer()),
        ):
            orch.run_query("what is alpha?")
    finally:
        direct_set_qmd_search(qmd_query)

    assert called == ["fake", "fake"]


def test_run_query_passes_full_page_bodies_not_excerpts(orch: Orchestrator) -> None:
    captured: dict[str, object] = {}

    def capture(_self: object, _prompt: str, **kwargs: object) -> mock.Mock:
        captured["deps"] = kwargs["deps"]
        return mock.Mock(output=_answer())

    with mock.patch.object(type(orch._query_synthesizer_agent), "run_sync", capture):
        orch.run_query("what is alpha?")

    deps = captured["deps"]
    assert deps.question == "what is alpha?"  # type: ignore[attr-defined]
    assert "Alpha is the first letter." in deps.page_texts["wiki/concepts/alpha.md"]  # type: ignore[attr-defined]
    assert "title: Alpha" in deps.page_texts["wiki/concepts/alpha.md"]  # type: ignore[attr-defined]
