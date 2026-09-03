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
    assert result.citations == ["wiki/concepts/alpha.md"]
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
    assert "Based on 1 wiki page(s)" in result.answer
    assert result.citations == ["wiki/concepts/alpha.md"]


def test_run_query_drops_citations_the_agent_never_received(orch: Orchestrator) -> None:
    hallucinated = _answer(citations=["wiki/concepts/alpha.md", "wiki/concepts/ghost.md"])
    with mock.patch.object(
        type(orch._query_synthesizer_agent),
        "run_sync",
        return_value=mock.Mock(output=hallucinated),
    ):
        result = orch.run_query("what is alpha?")

    assert result.synthesis_used is True
    assert result.citations == ["wiki/concepts/alpha.md"]
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
                ),
            ],
        )

    # Agent ran, returned its answer, no failure surfaced.
    assert output is not None
    assert reason == ""
    # The unreadable page was silently skipped; deps has no entry for it.
    assert captured["deps"].page_texts == {}  # type: ignore[attr-defined]


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
    """
    from lies.query import synthesize_answer
    from lies.query.synthesizer import retrieve_pages
    from lies.query.synthesizer import set_qmd_search as direct_set_qmd_search

    sentinel_calls: list[tuple[str, object, object, object]] = []

    def sentinel(_cwd: object, _question: object, _limit: object) -> list[dict[str, object]]:
        sentinel_calls.append(("sentinel", _cwd, _question, _limit))
        return [{"path": "concepts/alpha.md", "score": 0.9}]

    direct_set_qmd_search(sentinel)
    try:
        # Both functions honor the rebind when no kwarg is supplied.
        retrieve_pages("what is alpha?", orch.wiki)
        assert len(sentinel_calls) == 1
        synthesize_answer("what is alpha?", orch.wiki)
        assert len(sentinel_calls) == 2
    finally:
        # Restore the real binary so other tests aren't broken.
        direct_set_qmd_search(qmd_query)


def test_set_qmd_search_propagates_through_orchestrator(
    orch: Orchestrator,
) -> None:
    """`run_query` honors the indirection even though it doesn't pass
    the kwarg explicitly to ``retrieve_pages``.
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

    assert called == ["fake"]


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
