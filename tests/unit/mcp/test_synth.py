"""Tests for the synthesize MCP tool module."""

from __future__ import annotations

import pytest


def test_synthesize_envelope_shape():
    from lies.mcp.synth import SynthesizeEnvelope

    env = SynthesizeEnvelope(
        question="q",
        tag_expr=None,
        answer="a",
        citations=[],
        pages_read=[],
        fallback_used=False,
        synthesis_used=True,
    )
    assert env.question == "q"
    assert env.answer == "a"
    assert env.synthesis_used is True


async def test_synthesize_empty_digest_returns_honest_prose(monkeypatch):
    """When ground() returns no citations, synthesize returns prose
    explaining the gap, not an empty answer."""
    from lies.mcp import synth

    from lies.mcp.grounding import ArchivistDigest

    fake_digest = ArchivistDigest(
        question="q",
        tag_expr=None,
        exclude_expr=None,
        citations=[],
        no_coverage=True,
        distinct_pages=0,
        searched_scope=["switchyard"],
        no_library=False,
    )

    # ``synth.ground`` is the async archivist function; the test mock
    # mirrors that signature so synthesize() can ``await`` it.
    async def fake_ground(*args, **kwargs):
        return fake_digest

    monkeypatch.setattr(synth, "ground", fake_ground)

    env = await synth.synthesize("q")
    assert env.answer == "No relevant content found in library."
    assert env.pages_read == []
    assert env.citations == []
    assert env.fallback_used is True
    assert env.synthesis_used is False


def test_synthesize_file_back_raises_tool_error():
    import asyncio

    from fastmcp.exceptions import ToolError
    from lies.mcp import synth

    with pytest.raises(ToolError, match="file_back deferred"):
        asyncio.run(synth.synthesize("q", file_back=True))


async def _raise_busy_lock(*a: object, **kw: object) -> object:
    """A real ``QmdLockBusy``, carrying what the seam would have threaded in."""
    from lies.lock_errors import QmdLockBusy

    raise QmdLockBusy(
        holder_pid=4242,
        waited_s=120.0,
        max_s=120.0,
        lock_path="/var/lib/lies/qmd.lock",
    )


async def test_a_busy_qmd_lock_reaches_the_synthesize_envelope(monkeypatch):
    """The ``ground()`` / ``synthesize()`` path carries the diagnostics."""
    from lies.mcp import grounding, synth

    # A registered collection, or ``ground()`` takes the ``no_library``
    # fast-path and never reaches the fan-out this test is about.
    from lies.library import registry as reg_mod
    from lies.library.registry import LibraryCollectionMeta
    from lies.query import synthesizer as synth_mod

    metas = [LibraryCollectionMeta(name="alpha", tags=())]
    monkeypatch.setattr(reg_mod, "library_collection_metas", lambda: iter(metas))
    monkeypatch.setattr(synth_mod, "_all_collection_names", lambda: ["alpha"])
    monkeypatch.setattr(grounding, "_fanout_unscoped", _raise_busy_lock)
    monkeypatch.setattr(synth, "ground", grounding.ground)

    env = await synth.synthesize("q")

    assert env.fallback_used is True
    assert env.synthesis_used is False
    assert env.citations == []
    assert env.fallback_reason is not None
    reason = env.fallback_reason
    assert "qmd lock busy" in reason, reason
    assert "4242" in reason, f"the holder pid must reach the caller; got {reason!r}"
    assert "120" in reason, f"the budget must reach the caller; got {reason!r}"
    assert "/var/lib/lies/qmd.lock" in reason, (
        f"the lock path must reach the caller; got {reason!r}"
    )
    # And the corpus claim is still refused, exactly as before.
    assert "inconclusive" in env.answer


def test_lib_ask_returns_the_synthesizers_citations(monkeypatch: pytest.MonkeyPatch) -> None:
    """``citations`` is not dead on the success path."""
    from unittest.mock import MagicMock

    from lies.agents.librarian import LibrarianOutput, PageExcerpt
    from lies.markdown_spans import Span
    from lies.mcp import synth
    from lies.query.citation import Citation

    excerpt = PageExcerpt(
        collection="alpha",
        slug="alpha/page.md",
        title="Page",
        spans=[Span(heading_path=["H"], body="verbatim", code_fence=False, start_line=1)],
    )
    lib_out = LibrarianOutput(
        tag_expr=None,
        exclude_expr=None,
        excerpts=[excerpt],
        distinct_pages=1,
        no_coverage=False,
        searched_scope=["alpha"],
    )
    synth_out = MagicMock()
    synth_out.answer = "## Headline\n\nBody."
    synth_out.citations = ["alpha/page.md", "ghost/never-read.md"]
    synth_out.pages_read = ["alpha/page.md"]
    synth_out.fallback_used = False
    synth_out.synthesis_used = True
    synth_out.fallback_reason = None

    monkeypatch.setattr(synth, "librarian_agent_run", lambda deps: lib_out)
    monkeypatch.setattr(synth, "synthesizer_agent_run", lambda lo, q: synth_out)

    out = synth.lib_ask.fn(question="hi", tag_expr="c:alpha")

    assert out.citations == [
        Citation(path="alpha/page.md", source="library"),
        Citation(path="ghost/never-read.md", source="wiki"),
    ], out.citations
    assert out.synthesis_used is True


def test_a_reasonless_empty_bundle_renders_a_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """The no-excerpts branch reports WHY, not just that it happened."""
    from unittest.mock import MagicMock

    from lies.agents.librarian import LibrarianOutput
    from lies.mcp import synth

    for reason in (
        "unknown tag: 'c:nope'",
        "tag expression resolved to no collections: 'c:nope'",
        "collection 'alpha' is registered but has no documents in qmd",
        "no collections registered",
    ):
        lib_out = LibrarianOutput(
            tag_expr=None,
            exclude_expr=None,
            excerpts=[],
            distinct_pages=0,
            no_coverage=True,
            searched_scope=["alpha"],
            fallback_reason=reason,
        )
        monkeypatch.setattr(
            synth, "librarian_agent_run", lambda deps, _r=reason: _replace(lib_out, _r)
        )
        monkeypatch.setattr(synth, "synthesizer_agent_run", lambda lo, q: MagicMock())

        out = synth.lib_ask.fn(question="hi", tag_expr="c:alpha")

        assert out.fallback_reason == reason, (
            f"the envelope must name the cause; got {out.fallback_reason!r}"
        )
        # The corpus claim itself is unchanged — these dispatches ran.
        assert out.answer == "No relevant content found in library."
        assert out.fallback_used is True
        assert out.synthesis_used is False


def _replace(out, reason):
    """A ``LibrarianOutput`` with ``fallback_reason`` set."""
    import dataclasses

    return dataclasses.replace(out, fallback_reason=reason)


def test_an_empty_bundle_with_no_reason_keeps_the_fixed_sentence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The generic sentence survives as the ``or`` fallback."""
    from unittest.mock import MagicMock

    from lies.agents.librarian import LibrarianOutput
    from lies.mcp import synth

    lib_out = LibrarianOutput(
        tag_expr=None,
        exclude_expr=None,
        excerpts=[],
        distinct_pages=0,
        no_coverage=True,
        searched_scope=["alpha"],
        fallback_reason=None,
    )
    monkeypatch.setattr(synth, "librarian_agent_run", lambda deps: lib_out)
    monkeypatch.setattr(synth, "synthesizer_agent_run", lambda lo, q: MagicMock())

    out = synth.lib_ask.fn(question="hi", tag_expr="c:alpha")

    assert out.fallback_reason == "librarian returned no excerpts"


def test_citations_from_paths_is_the_one_conversion() -> None:
    """The orchestrator and ``lib_ask`` must not grow a second copy."""
    import ast
    import inspect

    import lies.orchestrator as orch_mod
    import lies.query.citation as citation_mod

    assert hasattr(citation_mod, "citations_from_paths")
    assert orch_mod.citations_from_paths is citation_mod.citations_from_paths, (
        "the orchestrator must import the shared conversion, not its own"
    )

    source = inspect.getsource(orch_mod)
    calls = [
        node
        for node in ast.walk(ast.parse(inspect.cleandoc(source)))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "citations_from_paths"
    ]
    assert calls, (
        "the orchestrator imports citations_from_paths but never calls it; "
        "the conversion has been duplicated inline, which is the drift "
        "this shared function exists to prevent"
    )


async def test_a_transient_failure_with_no_reason_keeps_the_fixed_string(monkeypatch):
    """The untyped arm still gets the generic sentence — the fix is additive."""
    from lies.mcp import synth
    from lies.mcp.grounding import ArchivistDigest

    async def _fake_ground(*a: object, **kw: object) -> ArchivistDigest:
        return ArchivistDigest(
            question="q",
            tag_expr=None,
            exclude_expr=None,
            citations=[],
            no_coverage=False,
            distinct_pages=0,
            searched_scope=["alpha"],
            transient=True,
        )

    monkeypatch.setattr(synth, "ground", _fake_ground)

    env = await synth.synthesize("q")

    assert env.fallback_reason == "ground() dispatch failed; no corpus claim made"
