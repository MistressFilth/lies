"""lib_ask() orchestrates librarian agent → synthesizer → envelope."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def _fake_librarian_output(
    *,
    searched_scope: list[str] | None = None,
    excerpts: list | None = None,
    distinct_pages: int = 0,
) -> MagicMock:
    out = MagicMock()
    out.tag_expr = "c:alpha"
    out.exclude_expr = None
    out.excerpts = excerpts or []
    out.distinct_pages = distinct_pages
    out.no_coverage = False
    out.searched_scope = searched_scope or []
    return out


@pytest.mark.slow
def test_ask_runs_librarian_then_synthesizer(monkeypatch: pytest.MonkeyPatch) -> None:
    """lib_ask() calls librarian_agent.run_sync then synthesizer_agent.run_sync."""
    from lies.mcp.synth import lib_ask

    lib_out = _fake_librarian_output(
        searched_scope=["alpha"],
        distinct_pages=1,
        excerpts=[MagicMock()],
    )
    monkeypatch.setattr(
        "lies.mcp.synth.librarian_agent",
        MagicMock(),
    )
    monkeypatch.setattr(
        "lies.mcp.synth.librarian_agent_run",
        lambda deps: lib_out,
    )
    synth_out = MagicMock()
    synth_out.answer = "## Headline\n\nBody."
    synth_out.pages_read = ["alpha/page.md"]
    synth_out.fallback_used = False
    synth_out.synthesis_used = True
    synth_out.fallback_reason = None
    monkeypatch.setattr(
        "lies.mcp.synth.synthesizer_agent_run",
        lambda lib_out, question: synth_out,
    )
    monkeypatch.setattr("lies.mcp.synth._resolve_synthesizer_model", lambda: "test-model")

    # Call via FastMCP-resolved function attr
    out = lib_ask.fn(question="hi", tag_expr="c:alpha")

    assert out.answer == "## Headline\n\nBody."
    assert out.pages_read == ["alpha/page.md"]
    assert out.searched_scope == ["alpha"]


def test_ask_empty_librarian_output_returns_no_coverage_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty librarian output → honest gap envelope, no LLM call."""
    from lies.mcp.synth import lib_ask

    lib_out = _fake_librarian_output(searched_scope=["alpha"], distinct_pages=0, excerpts=[])
    monkeypatch.setattr("lies.mcp.synth.librarian_agent_run", lambda deps: lib_out)
    synth_called = []
    monkeypatch.setattr(
        "lies.mcp.synth.synthesizer_agent_run",
        lambda lib_out, q: synth_called.append(True) or MagicMock(),
    )

    out = lib_ask.fn(question="hi", tag_expr="c:alpha")
    assert out.answer == "No relevant content found in library."
    assert out.synthesis_used is False
    assert out.fallback_used is True
    assert "no excerpts" in (out.fallback_reason or "").lower()
    assert synth_called == []


def test_ask_file_back_raises_tool_error() -> None:
    """file_back=True is still deferred to v0.41."""
    from fastmcp.exceptions import ToolError

    from lies.mcp.synth import lib_ask

    import pytest

    with pytest.raises(ToolError, match="file_back deferred"):
        lib_ask.fn(question="hi", file_back=True)


def test_ask_envelope_propagates_searched_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """envelope.searched_scope mirrors librarian_output.searched_scope."""
    from lies.mcp.synth import lib_ask

    lib_out = _fake_librarian_output(searched_scope=["alpha", "beta"], distinct_pages=2)
    monkeypatch.setattr("lies.mcp.synth.librarian_agent_run", lambda deps: lib_out)
    synth_out = MagicMock()
    synth_out.answer = "Body."
    synth_out.pages_read = []
    synth_out.fallback_used = False
    synth_out.synthesis_used = True
    synth_out.fallback_reason = None
    monkeypatch.setattr("lies.mcp.synth.synthesizer_agent_run", lambda lo, q: synth_out)
    monkeypatch.setattr("lies.mcp.synth._resolve_synthesizer_model", lambda: "test-model")

    out = lib_ask.fn(question="hi", tag_expr="c:alpha|c:beta")
    assert out.searched_scope == ["alpha", "beta"]


def test_ask_returns_no_coverage_envelope_when_librarian_dispatch_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """librarian_agent.run_sync raising → lib_ask() returns gap envelope, no crash.

    Reproduces the v0.40 live-test failure: pydantic-ai raises
    ``UsageLimitExceeded("Exceeded maximum output retries (1)")`` when the
    librarian LLM cannot produce a valid ``LibrarianOutput``. The MCP
    ``lib_ask`` tool must surface an honest gap envelope instead of
    propagating the exception. Mirrors ``test_ground_returns_empty_digest_on_librarian_exception``
    in :mod:`tests.integration.test_grounding_unit` for the new lib_ask path.
    """
    from lies.mcp import synth

    class _BoomAgent:
        def run_sync(self, user_prompt, *, deps):
            raise RuntimeError("Exceeded maximum output retries (1)")

    # Patch the agent factory so ``librarian_agent_run`` builds a BoomAgent.
    monkeypatch.setattr(synth, "librarian_agent", lambda model=None: _BoomAgent())
    # Model resolution + tool registration are no-ops in this test — only
    # the ``run_sync`` raise matters. ``register_librarian_tools`` is
    # imported lazily inside ``librarian_agent_run`` so we patch at the
    # source module to catch the local rebinding.
    monkeypatch.setattr(synth, "_resolve_librarian_model", lambda: "test-model")
    monkeypatch.setattr("lies.agents.librarian.register_librarian_tools", lambda agent: None)
    # The synthesizer must NOT be reached on the fallback path.
    synth_called: list[bool] = []
    monkeypatch.setattr(
        synth,
        "synthesizer_agent_run",
        lambda lib_out, q: synth_called.append(True) or MagicMock(),
    )

    with caplog.at_level("WARNING", logger="lies.mcp.synth"):
        out = synth.lib_ask.fn(question="what is pydantic?", tag_expr="c:alpha")

    assert out.answer == "No relevant content found in library."
    assert out.synthesis_used is False
    assert out.fallback_used is True
    assert "no excerpts" in (out.fallback_reason or "").lower()
    assert out.searched_scope == []
    assert synth_called == []
    # The operator should see one warning explaining the fallback fired.
    assert any(
        "librarian_agent_run" in record.getMessage() and "RuntimeError" in record.getMessage()
        for record in caplog.records
    ), f"expected fallback warning, got: {[r.getMessage() for r in caplog.records]}"
