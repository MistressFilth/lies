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

    with pytest.raises(ToolError, match="file_back is deferred"):
        asyncio.run(synth.synthesize("q", file_back=True))
