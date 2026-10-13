"""The orchestrator's once-registered tools attest per dispatch, not per agent."""

from __future__ import annotations

import dataclasses
import threading
from typing import Any

import pytest
from pydantic_ai import RunContext
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from lies.agents.librarian import (
    LibrarianDeps,
    LibrarianOutput,
    PageExcerpt,
    SearchDiagnostics,
    current_search_diagnostics,
    librarian_agent,
    register_librarian_tools,
    search_diagnostics_scope,
)
from lies.markdown_spans import Span

REAL_PATH = "alpha/real.md"
INVENTED_PATH = "alpha/never-returned.md"


def _bundle(*slugs: str) -> LibrarianOutput:
    excerpts = [
        PageExcerpt(
            collection=slug.split("/", 1)[0],
            slug=slug,
            title=slug.rsplit("/", 1)[-1],
            spans=[
                Span(
                    heading_path=["Real"],
                    body="The daemon binds its backend at construction.",
                    code_fence=False,
                    start_line=1,
                )
            ],
        )
        for slug in slugs
    ]
    return LibrarianOutput(
        tag_expr=None,
        exclude_expr=None,
        excerpts=excerpts,
        distinct_pages=len(slugs),
        no_coverage=False,
    )


def _model(hit_paths: list[str]) -> FunctionModel:
    """Call ``search`` once per path, then emit a bundle naming them all."""
    turns = {"n": 0}

    def _respond(messages: list, info: AgentInfo) -> ModelResponse:
        turns["n"] += 1
        if turns["n"] <= len(hit_paths):
            return ModelResponse(
                parts=[ToolCallPart("search", {"question": hit_paths[turns["n"] - 1]})]
            )
        bundle = _bundle(*hit_paths)
        return ModelResponse(parts=[ToolCallPart("final_result", dataclasses.asdict(bundle))])

    return FunctionModel(_respond)


def _witness_agent(hit_paths: list[str], seen: list[SearchDiagnostics | None]) -> Any:
    """A librarian whose ``search`` closure witnesses *and* records."""
    agent = librarian_agent(model=_model(hit_paths))

    def _search(ctx: RunContext[LibrarianDeps], question: str) -> dict[str, Any]:
        envelope = {"hits": [{"path": question}], "fallback_reason": None}
        record = current_search_diagnostics()
        seen.append(record)
        if record is not None:
            record.record(envelope)
        return envelope

    agent.tool(name="search")(_search)
    return agent


def _deps() -> LibrarianDeps:
    return LibrarianDeps(question="what does the daemon bind?", tag_expr=None, exclude_expr=None)


def test_the_tools_see_the_records_bound_around_run_sync() -> None:
    """The closure reads the record; nothing captures it at registration."""
    seen: list[SearchDiagnostics | None] = []
    agent = _witness_agent([REAL_PATH], seen)
    record = SearchDiagnostics()
    with search_diagnostics_scope(record):
        agent.run_sync("q", deps=_deps())
    assert seen == [record], (
        "the tool closure ran in a context that could not see the caller's "
        f"ContextVar; it saw {seen!r}, bound was {record!r}"
    )


def test_the_tools_see_the_record_from_a_worker_thread_dispatch() -> None:
    """``run_sync`` from a worker thread — the shape the MCP server drives."""
    seen: list[SearchDiagnostics | None] = []
    agent = _witness_agent([REAL_PATH], seen)
    record = SearchDiagnostics()

    def dispatch() -> None:
        with search_diagnostics_scope(record):
            agent.run_sync("q", deps=_deps())

    thread = threading.Thread(target=dispatch, name="record-witness")
    thread.start()
    thread.join(timeout=30)
    assert seen == [record], seen


def test_two_overlapping_dispatches_do_not_bleed_into_each_other() -> None:
    """Two threads, barrier-synchronised, each record keeps its own hits."""
    barrier = threading.Barrier(2)
    paths = {"one": "alpha/one.md", "two": "beta/two.md"}
    records = {key: SearchDiagnostics() for key in paths}
    seen: dict[str, list[SearchDiagnostics | None]] = {key: [] for key in paths}

    def dispatch(key: str) -> None:
        agent = _witness_agent([paths[key]], seen[key])
        with search_diagnostics_scope(records[key]):
            barrier.wait(timeout=30)
            agent.run_sync("q", deps=_deps())
            barrier.wait(timeout=30)

    threads = [threading.Thread(target=dispatch, args=(key,)) for key in paths]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    for key in paths:
        assert seen[key] == [records[key]], f"{key}: {seen[key]!r}"
        assert records[key].attested_paths == {paths[key]}, records[key].attested_paths
    assert records["one"] is not records["two"]


def test_a_dispatch_with_no_record_bound_is_untouched() -> None:
    """The no-record arm behaves exactly as it did before the ContextVar."""
    seen: list[SearchDiagnostics | None] = []
    agent = _witness_agent([REAL_PATH], seen)
    out = agent.run_sync("q", deps=_deps()).output
    assert seen == [None], seen
    assert [e.slug for e in out.excerpts] == [REAL_PATH], out.excerpts


def test_the_scope_restores_the_previous_binding() -> None:
    """A nested dispatch cannot leave its record behind."""
    outer = SearchDiagnostics()
    with search_diagnostics_scope(outer):
        with search_diagnostics_scope(SearchDiagnostics()):
            pass
        assert current_search_diagnostics() is outer
    assert current_search_diagnostics() is None


def test_an_explicit_record_argument_is_not_shadowed_by_the_ambient_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ground``'s legacy path passes one explicitly; it must still win."""
    explicit = SearchDiagnostics()
    ambient = SearchDiagnostics()
    agent = librarian_agent(model=_model([REAL_PATH]))
    register_librarian_tools(agent, search_diagnostics=explicit)
    monkeypatch.setattr(
        "lies.mcp.search.search",
        lambda **kw: {"hits": [{"path": REAL_PATH}], "fallback_reason": None},
    )
    with search_diagnostics_scope(ambient):
        agent.run_sync("q", deps=_deps())
    assert explicit.attested_paths == {REAL_PATH}, explicit.attested_paths
    assert ambient.attested_paths == set(), (
        "the ambient record was written to even though the tool was given an "
        "explicit one; a per-dispatch argument must never be overridden"
    )


@pytest.fixture
def orchestrator(tmp_path: Any) -> Any:
    """A constructed Orchestrator; test models, isolated XDG roots."""
    from lies.orchestrator import Orchestrator
    from tests.conftest import make_wiki, models_for_tests

    (tmp_path / "raw").mkdir(exist_ok=True)
    (tmp_path / "wiki").mkdir(exist_ok=True)
    wiki = make_wiki(name="record-test", data_root=tmp_path)
    return Orchestrator(wiki=wiki, models=models_for_tests("test"))


def test_the_orchestrator_drops_a_fabricated_excerpt(orchestrator: Any) -> None:
    """The once-registered tools record, and the overlay drops the invention."""
    seen: list[SearchDiagnostics | None] = []

    def fake_run_sync(question: str, deps: Any = None) -> Any:
        agent = _witness_agent([REAL_PATH], seen)
        agent.run_sync(question, deps=deps)
        return type("R", (), {"output": _bundle(INVENTED_PATH)})()

    orchestrator._librarian_agent = type("A", (), {"run_sync": staticmethod(fake_run_sync)})()

    out = orchestrator._dispatch_librarian("q", _deps())

    assert seen and seen[0] is not None, (
        "the orchestrator's dispatch did not bind a record the tools could see"
    )
    assert out.excerpts == [], [e.slug for e in out.excerpts]
    assert INVENTED_PATH in (out.fallback_reason or ""), out.fallback_reason
    assert out.transient is True
    assert out.no_coverage is False


def test_the_orchestrator_keeps_a_real_excerpt(orchestrator: Any) -> None:
    """The other end: the rule must not cost the orchestrator a real answer."""
    seen: list[SearchDiagnostics | None] = []

    def fake_run_sync(question: str, deps: Any = None) -> Any:
        agent = _witness_agent([REAL_PATH], seen)
        agent.run_sync(question, deps=deps)
        return type("R", (), {"output": _bundle(REAL_PATH)})()

    orchestrator._librarian_agent = type("A", (), {"run_sync": staticmethod(fake_run_sync)})()

    out = orchestrator._dispatch_librarian("q", _deps())

    assert [e.slug for e in out.excerpts] == [REAL_PATH], out.excerpts
    assert out.fallback_reason is None, out.fallback_reason


def test_a_canned_query_answer_rides_through_un_attested(orchestrator: Any) -> None:
    """Integration tests patch ``run_sync`` to a ``QueryAnswer``; keep that shape."""
    from lies.agents.query_synthesizer import QueryAnswer

    canned = QueryAnswer(answer="canned", citations=[], should_file=False)
    orchestrator._librarian_agent = type(
        "A",
        (),
        {"run_sync": staticmethod(lambda question, deps=None: type("R", (), {"output": canned})())},
    )()

    out = orchestrator._dispatch_librarian("q", _deps())

    assert out is canned
