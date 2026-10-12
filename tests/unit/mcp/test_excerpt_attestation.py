"""A bundle's excerpts are checked against what the tools actually returned."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

# Imported at module scope, deliberately: ``lies.mcp.synth`` and
# ``lies.agents.librarian`` are a few hundred milliseconds of import on a
# cold interpreter, and the per-test budget gate measures the *call*
# phase (see the note in ``test_ask_lock_contention.py``). None of these
# read the library registry at import time, so importing them outside the
# per-test XDG isolation fixture is safe.
from lies.agents.librarian import LibrarianOutput, PageExcerpt, librarian_agent  # noqa: E402
from lies.markdown_spans import Span  # noqa: E402
from lies.mcp import synth as synth_mod  # noqa: E402

HIT_PATH = "alpha/real.md"
READ_ONLY_PATH = "alpha/linked-from-a-body.md"
INVENTED_PATH = "alpha/never-returned-by-anything.md"

BODY = {
    HIT_PATH: "# Real\n\nThe daemon binds its backend at construction.\n",
    READ_ONLY_PATH: "# Linked\n\nA body the model reached by following a link.\n",
}


def _span(path: str) -> dict[str, Any]:
    return {
        "heading_path": ["Real"],
        "body": BODY.get(path, "a passage that was never returned by any tool"),
        "code_fence": False,
        "start_line": 1,
    }


def _excerpt_args(path: str) -> dict[str, Any]:
    return {
        "collection": path.split("/", 1)[0],
        "slug": path,
        "title": path.rsplit("/", 1)[-1],
        "spans": [_span(path)],
        "source_kind": "library",
    }


def _bundle(*paths: str) -> LibrarianOutput:
    return LibrarianOutput(
        tag_expr="c:alpha",
        exclude_expr=None,
        excerpts=[_excerpt_args(p) for p in paths],
        distinct_pages=len(paths),
        no_coverage=False,
        searched_scope=["alpha"],
    )


def _model(tool_calls: list[ToolCallPart], bundle: LibrarianOutput) -> FunctionModel:
    """Emit ``tool_calls``, then the bundle as the agent's final output."""
    turns = {"n": 0}

    def _respond(messages: list, info: AgentInfo) -> ModelResponse:
        turns["n"] += 1
        if turns["n"] <= len(tool_calls):
            return ModelResponse(parts=[tool_calls[turns["n"] - 1]])
        return ModelResponse(parts=[ToolCallPart("final_result", dataclasses.asdict(bundle))])

    model = FunctionModel(_respond)
    model.lies_turns = turns  # type: ignore[attr-defined]
    return model


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    bundle: LibrarianOutput,
    hits: tuple[str, ...] = (HIT_PATH,),
    read_paths: list[str] | None = None,
    read_result: Any = None,
) -> FunctionModel:
    """Wire a librarian that searches, reads and returns ``bundle``."""
    calls = [ToolCallPart("search", {"question": "what does the daemon bind?"})]
    if read_paths is not None:
        calls.append(ToolCallPart("read", {"paths": read_paths}))

    def _search(**kwargs: Any) -> dict[str, Any]:
        return {
            "hit": (
                {"path": hits[0], "title": "t", "score": 0.9, "snippet": "s"} if hits else None
            ),
            "hits": [
                {"path": p, "title": p.rsplit("/", 1)[-1], "score": 0.9, "snippet": "snip"}
                for p in hits
            ],
            "unknown_tags": [],
            "no_coverage": False,
            "transient": False,
            "searched_scope": ["alpha"],
            "fallback_reason": None,
        }

    def _read(paths: list[str]) -> Any:
        if read_result is not None:
            return read_result
        return {p: BODY.get(p, "") for p in paths if p in BODY}

    monkeypatch.setattr("lies.mcp.search.search", _search)
    monkeypatch.setattr("lies.mcp.read.read", _read)

    model = _model(calls, bundle)
    # The resolved string is swallowed, not forwarded: the factory is
    # called as ``agent_factory(model=resolved_string)`` and a parameter
    # that accepted it would hand the string to a real provider lookup
    # ("Unknown model: test-model"). Same trap, same fix as
    # ``test_ask_lock_contention.py``.
    monkeypatch.setattr(synth_mod, "librarian_agent", lambda **_: librarian_agent(model=model))
    monkeypatch.setattr(synth_mod, "_resolve_librarian_model", lambda: "test-model")
    return model


def _run() -> LibrarianOutput:
    return synth_mod.librarian_agent_run(
        synth_mod._build_query_deps(
            question="what does the daemon bind?", tag_expr=None, exclude_tags=None
        )
    )


def test_a_fabricated_excerpt_is_dropped_and_the_reason_names_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One real path, one invented one. The invented one does not survive."""
    _install(
        monkeypatch,
        bundle=_bundle(HIT_PATH, INVENTED_PATH),
        read_paths=[HIT_PATH],
    )

    out = _run()

    assert [e.slug for e in out.excerpts] == [HIT_PATH], (
        f"the fabricated excerpt survived: {[e.slug for e in out.excerpts]}"
    )
    reason = out.fallback_reason or ""
    assert INVENTED_PATH in reason, reason
    assert "dropped" in reason, reason
    # A drop is not a corpus claim, and not a dispatch failure either.
    assert out.no_coverage is False
    assert out.transient is False
    # distinct_pages must track what is left, not what the model claimed.
    assert out.distinct_pages == 1


def test_an_excerpt_read_but_never_hit_survives(monkeypatch: pytest.MonkeyPatch) -> None:
    """The oracle is hits ∪ reads, and this is why."""
    _install(
        monkeypatch,
        bundle=_bundle(HIT_PATH, READ_ONLY_PATH),
        read_paths=[HIT_PATH, READ_ONLY_PATH],
    )

    out = _run()

    assert [e.slug for e in out.excerpts] == [HIT_PATH, READ_ONLY_PATH], (
        "a page the read tool really returned was dropped; the oracle is "
        "'a tool returned this path', not 'the envelope named it'"
    )
    assert out.fallback_reason is None, out.fallback_reason
    assert out.distinct_pages == 2


def test_excerpts_built_from_the_search_snippets_alone_survive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The contract's snippet-only arm: search, no read, answer anyway."""
    _install(monkeypatch, bundle=_bundle(HIT_PATH), read_paths=None)

    out = _run()

    assert [e.slug for e in out.excerpts] == [HIT_PATH]
    assert out.fallback_reason is None, out.fallback_reason


def test_a_bundle_with_no_recorded_tool_result_at_all_is_emptied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The purest fabrication: the model never called a tool."""
    bundle = _bundle(HIT_PATH, READ_ONLY_PATH, INVENTED_PATH)
    monkeypatch.setattr("lies.mcp.search.search", lambda **kw: {"hits": []})
    monkeypatch.setattr("lies.mcp.read.read", lambda paths: {})
    model = _model([], bundle)
    monkeypatch.setattr(synth_mod, "librarian_agent", lambda **_: librarian_agent(model=model))
    monkeypatch.setattr(synth_mod, "_resolve_librarian_model", lambda: "test-model")

    out = _run()

    assert model.lies_turns["n"] == 1, "the model should have answered without calling a tool"
    assert out.excerpts == []
    reason = out.fallback_reason or ""
    assert "3 of the librarian's excerpt(s)" in reason, reason
    assert INVENTED_PATH in reason, reason
    assert out.no_coverage is False, (
        "a bundle whose every excerpt was fabricated is a fact about the "
        "run, never a statement that the library has nothing"
    )


def test_the_unresolvable_paths_key_is_not_attested(monkeypatch: pytest.MonkeyPatch) -> None:
    """``read`` reports unresolved paths under ``"_missing"``."""
    _install(
        monkeypatch,
        bundle=_bundle(HIT_PATH, READ_ONLY_PATH, "_missing"),
        read_paths=[HIT_PATH, READ_ONLY_PATH],
        read_result={HIT_PATH: BODY[HIT_PATH], "_missing": [READ_ONLY_PATH]},
    )

    out = _run()

    assert [e.slug for e in out.excerpts] == [HIT_PATH], (
        "a path the read tool reported as unresolvable must not be "
        "citable — the body for it never existed — and neither must the "
        "synthetic key itself, which is not a page"
    )
    reason = out.fallback_reason or ""
    assert READ_ONLY_PATH in reason, reason
    assert "_missing" in reason, reason


def test_the_envelope_renders_inconclusive_for_an_entirely_fabricated_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end through ``lib_ask``, which is where the claim lands."""
    _install(monkeypatch, bundle=_bundle(INVENTED_PATH), read_paths=[HIT_PATH])
    synthesizer_calls: list[str] = []
    monkeypatch.setattr(
        synth_mod,
        "synthesizer_agent_run",
        lambda lib_out, q: synthesizer_calls.append(q) or None,
    )

    envelope = synth_mod.lib_ask.fn(question="what does the daemon bind?", tag_expr=None)

    assert envelope.citations == [], envelope.citations
    assert "No relevant content found in library." not in envelope.answer, envelope.answer
    assert "inconclusive" in envelope.answer, envelope.answer
    assert INVENTED_PATH in (envelope.fallback_reason or ""), envelope.fallback_reason
    assert synthesizer_calls == [], "the synthesizer must not run on an invented bundle"
    assert envelope.synthesis_used is False


class _SynthAnswer:
    """Minimal stand-in for the synthesizer's ``QueryAnswer``."""

    def __init__(self, path: str) -> None:
        self.answer = f"The backend is bound at construction. [{path}]"
        self.citations = [path]
        self.pages_read = [path]
        self.fallback_used = False
        self.fallback_reason = None


def test_the_attested_bundle_still_reaches_the_synthesizer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other end: the rule must not cost a real answer."""
    _install(monkeypatch, bundle=_bundle(HIT_PATH), read_paths=[HIT_PATH])
    seen: list[list[str]] = []
    monkeypatch.setattr(
        synth_mod,
        "synthesizer_agent_run",
        lambda lib_out, q: (
            seen.append([e.slug for e in lib_out.excerpts]) or _SynthAnswer(HIT_PATH)
        ),
    )

    envelope = synth_mod.lib_ask.fn(question="what does the daemon bind?", tag_expr=None)

    assert seen == [[HIT_PATH]], seen
    assert [c.path for c in envelope.citations] == [HIT_PATH], envelope.citations
    assert envelope.synthesis_used is True
    assert envelope.fallback_reason is None, envelope.fallback_reason


def test_a_span_body_is_still_not_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invented text attributed to a page that really was read still passes."""
    bundle = LibrarianOutput(
        tag_expr="c:alpha",
        exclude_expr=None,
        excerpts=[
            PageExcerpt(
                collection="alpha",
                slug=HIT_PATH,
                title="Real",
                spans=[
                    Span(
                        heading_path=["Real"],
                        body="NOT IN THE BODY AT ALL",
                        code_fence=False,
                        start_line=1,
                    )
                ],
                source_kind="library",
            )
        ],
        distinct_pages=1,
        no_coverage=False,
        searched_scope=["alpha"],
    )

    _install(monkeypatch, bundle=bundle, read_paths=[HIT_PATH])

    out = _run()

    assert [e.slug for e in out.excerpts] == [HIT_PATH], (
        "this test documents that text is NOT verified. If it fails, the "
        "text-level check landed -- update the AGENTS.md trade paragraph "
        "with it."
    )


def test_a_run_that_never_searched_keeps_its_transient_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty bundle must not be affected by the attestation arm."""
    bundle = LibrarianOutput(
        tag_expr="c:alpha",
        exclude_expr=None,
        excerpts=[],
        distinct_pages=0,
        no_coverage=False,
        searched_scope=[],
        transient=True,
        fallback_reason="librarian dispatch failed: RuntimeError: boom",
    )
    _install(monkeypatch, bundle=bundle)

    out = _run()

    assert out.excerpts == []
    assert out.transient is True, (
        "an empty bundle with nothing recorded must keep its own verdict; "
        "the attestation arm does not touch a bundle with no excerpts"
    )
    assert (out.fallback_reason or "") == bundle.fallback_reason
    assert out.no_coverage is False


def test_the_record_is_per_dispatch_and_does_not_leak_across_calls() -> None:
    """``attested_paths`` lives on the record, not on a module global."""
    from lies.agents.librarian import SearchDiagnostics

    first = SearchDiagnostics()
    second = SearchDiagnostics()
    first.record_read({"alpha/one.md": "body"})

    assert first.attested_paths == {"alpha/one.md"}
    assert second.attested_paths == set(), (
        "a second dispatch's record saw the first dispatch's attestation; "
        "the record must be an argument, never shared state"
    )


def test_the_hits_are_recorded_from_both_hit_and_hits() -> None:
    """The top-1 row and the ranked rows are both attested."""
    from lies.agents.librarian import SearchDiagnostics

    diag = SearchDiagnostics()
    diag.record(
        {
            "hit": {"path": "alpha/top1.md"},
            "hits": [{"path": "alpha/ranked.md"}, "alpha/bare-string.md"],
        }
    )

    assert diag.attested_paths == {"alpha/top1.md", "alpha/ranked.md", "alpha/bare-string.md"}


def test_a_malformed_envelope_attests_nothing_rather_than_coercing() -> None:
    """``None`` must not become the page ``"None"``."""
    from lies.agents.librarian import SearchDiagnostics

    diag = SearchDiagnostics()
    diag.record({"hits": [{"title": "no path"}, {"path": None}, 17, "alpha/real.md"]})

    assert diag.attested_paths == {"alpha/real.md"}


def test_a_non_dict_read_result_is_ignored() -> None:
    """The recorder must not raise on a tool that returned something else."""
    from lies.agents.librarian import SearchDiagnostics

    diag = SearchDiagnostics()
    diag.record_read("not a dict")  # type: ignore[arg-type]
    diag.record_read(None)  # type: ignore[arg-type]

    assert diag.attested_paths == set()
