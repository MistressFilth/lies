"""SynthesizeEnvelope carries additive searched_scope field."""

from __future__ import annotations

from lies.mcp.synth import SynthesizeEnvelope


def test_envelope_defaults_searched_scope_to_empty_list() -> None:
    """Back-compat: existing call sites that don't pass searched_scope get []."""
    env = SynthesizeEnvelope(
        question="q",
        tag_expr=None,
        answer="a",
        citations=[],
        pages_read=[],
        fallback_used=False,
        synthesis_used=True,
        fallback_reason=None,
    )
    assert env.searched_scope == []


def test_envelope_carries_searched_scope_explicitly() -> None:
    env = SynthesizeEnvelope(
        question="q",
        tag_expr="c:alpha",
        answer="a",
        citations=[],
        pages_read=[],
        fallback_used=False,
        synthesis_used=True,
        fallback_reason=None,
        searched_scope=["alpha"],
    )
    assert env.searched_scope == ["alpha"]
