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
    # (library + wiki) — both resolve to the same wiki page.
    # (Important 5 dedup collapses these to a single entry; the
    # dedup is asserted separately in test_retrieve_pages.py.)
    assert "Based on 2 wiki page(s)" in result.answer
    from lies.query.citation import Citation

    assert result.citations == [
        Citation(path="wiki/concepts/alpha.md", source="wiki"),
        Citation(path="wiki/concepts/alpha.md", source="wiki"),
    ]


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
            excerpt="Alpha is the first letter.",
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


def test_call_query_synthesizer_handles_unreadable_pages_silently(
    orch: Orchestrator,
) -> None:
    """An unreadable page must be silently skipped, not raised.

    Mirrors `_call_linter`'s defensive read loop: a single
    `OSError` / `UnicodeDecodeError` on one page must not bubble out of
    `_call_query_synthesizer` and crash the synthesis path. The agent
    still runs with the pages that did read cleanly.
    """

    captured: dict[str, object] = {}

    def capture(_self: object, _prompt: str, **kwargs: object) -> mock.Mock:
        captured["deps"] = kwargs["deps"]
        return mock.Mock(output=_answer())

    with (
        mock.patch.object(type(orch._query_synthesizer_agent), "run_sync", capture),
        mock.patch.object(Path, "read_text", side_effect=OSError("disk gone")),
    ):
        output, reason = orch._call_query_synthesizer(
            "what is alpha?",
            [
                PageRead(
                    rel_path="wiki/concepts/alpha.md",
                    title="Alpha",
                    excerpt="Alpha is the first letter.",
                    source="wiki",
                ),
            ],
        )

    # Agent ran, returned its answer, no failure surfaced.
    assert output is not None
    assert reason == ""
    # The unreadable page was silently skipped; deps has no entry for it.
    assert captured["deps"].page_texts == {}  # type: ignore[attr-defined]
    assert captured["deps"].page_sources == {}  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Critical 1 + Critical 4: path resolver branches on ``source`` and the
# deps carry a per-path source discriminator.
# ---------------------------------------------------------------------------


def test_call_query_synthesizer_reads_library_pages_from_collections_root(
    orch: Orchestrator,
) -> None:
    """A library-sourced page is resolved against
    ``Library.open().collections_root`` — not ``wiki.data_root``.

    Without the fix, the resolver joins ``self.wiki.data_root / rel_path``
    and reads nothing (the file lives under the library collections root).
    """
    import os
    from unittest import mock as _mock

    from lies.library.paths import Library

    Library.open.cache_clear()
    lib = Library.open()
    rel_path = "claude_platform/skills.md"
    lib_root = lib.collections_root
    (lib_root / "claude_platform").mkdir(parents=True, exist_ok=True)
    (lib_root / "claude_platform" / "skills.md").write_text(
        "# Skills\n\nLibrary body.\n", encoding="utf-8"
    )

    captured: dict[str, object] = {}

    def capture(_self: object, _prompt: str, **kwargs: object) -> _mock.Mock:
        captured["deps"] = kwargs["deps"]
        return _mock.Mock(output=_answer(citations=[rel_path]))

    try:
        with _mock.patch.object(type(orch._query_synthesizer_agent), "run_sync", capture):
            output, reason = orch._call_query_synthesizer(
                "anything",
                [
                    PageRead(
                        rel_path=rel_path,
                        title="Skills",
                        excerpt="Library body.",
                        source="library",
                    ),
                ],
            )
        assert output is not None
        assert reason == ""
        deps = captured["deps"]
        # The library page text is the file body — proving the resolver
        # landed at the right path (Library.open().collections_root/<rel>).
        assert deps.page_texts[rel_path] == "# Skills\n\nLibrary body.\n"  # type: ignore[attr-defined]
        assert deps.page_sources[rel_path] == "library"  # type: ignore[attr-defined]
    finally:
        # Restore the env so other tests aren't disturbed.
        for key in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
            if key in os.environ:
                del os.environ[key]


def test_call_query_synthesizer_reads_wiki_pages_from_data_root(
    orch: Orchestrator,
) -> None:
    """A wiki-sourced page is resolved against ``self.wiki.data_root``.

    ``wiki/concepts/alpha.md`` is a data_root-relative path with the
    ``wiki/`` prefix; the resolver joins onto ``wiki.data_root`` (which
    is one segment above ``wiki.wiki_dir``) so the prefix is preserved.
    Joining onto ``wiki_dir`` would silently produce
    ``wiki/wiki/...`` and read nothing.
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
                    excerpt="Alpha is the first letter.",
                    source="wiki",
                ),
            ],
        )

    assert output is not None
    assert reason == ""
    deps = captured["deps"]
    assert deps.page_texts["wiki/concepts/alpha.md"] == (  # type: ignore[attr-defined]
        "---\ntitle: Alpha\n---\n\nAlpha is the first letter.\n"
    )
    assert deps.page_sources["wiki/concepts/alpha.md"] == "wiki"  # type: ignore[attr-defined]


def test_call_query_synthesizer_silently_skips_unreadable_library_pages(
    orch: Orchestrator,
) -> None:
    """A library-sourced page whose file is missing is skipped, not crashed on.

    Mirrors the wiki-side defensive read loop: ``OSError`` /
    ``FileNotFoundError`` on one page must not bubble out of
    ``_call_query_synthesizer``. The agent still runs with whatever did
    read cleanly."""
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
                    excerpt="missing",
                    source="library",
                ),
            ],
        )

    assert output is not None
    assert reason == ""
    assert captured["deps"].page_texts == {}  # type: ignore[attr-defined]
    assert captured["deps"].page_sources == {}  # type: ignore[attr-defined]


def test_call_query_synthesizer_populates_page_sources_for_each_read_page(
    orch: Orchestrator,
) -> None:
    """Every successfully-read page appears in both ``page_texts`` and
    ``page_sources`` so the LLM prompt carries the discriminator for
    every page the agent sees."""
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
                    excerpt="Alpha is the first letter.",
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
