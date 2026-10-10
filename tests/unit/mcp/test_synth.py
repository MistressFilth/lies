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


async def test_a_busy_qmd_lock_reaches_the_lib_ask_envelope(monkeypatch):
    """The holder pid, the budget and the lock path survive to ``lib_ask``.

    This is the whole point of threading ``fallback_reason``. The
    transient branch used to emit one fixed sentence — "ground()
    dispatch failed; no corpus claim made" — for *every* dispatch
    failure, so a caller who queued behind a 120 s single-flight hold was
    told exactly what a caller whose daemon had wedged was told. Both
    answers are correct about the corpus (neither is a statement about
    it) and both are useless to an operator, who needs "wait" in one case
    and "restart the daemon" in the other.

    Driven through the real ``ground()`` rather than a hand-built digest,
    so the assertion covers the whole hop: lock raise → grounding arm →
    digest field → envelope field. Stubbing ``ground`` would leave the
    one link that actually broke untested.
    """
    from lies.library import registry as reg_mod
    from lies.library.registry import LibraryCollectionMeta
    from lies.mcp import grounding, synth
    from lies.query import synthesizer as synth_mod

    # A registered collection, or ``ground()`` takes the ``no_library``
    # fast-path and never reaches the fan-out this test is about.
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


async def test_a_transient_failure_with_no_reason_keeps_the_fixed_string(monkeypatch):
    """The untyped arm still gets the generic sentence — the fix is additive.

    A digest with no reason must not render "None", and a reader who hit
    an unnamed failure should get the same sentence they got before.
    """
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
