"""lib_ask() orchestrates librarian agent → synthesizer → envelope."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def _fake_librarian_output(
    *,
    searched_scope: list[str] | None = None,
    excerpts: list | None = None,
    distinct_pages: int = 0,
    transient: bool = False,
) -> MagicMock:
    out = MagicMock()
    out.tag_expr = "c:alpha"
    out.exclude_expr = None
    out.excerpts = excerpts or []
    out.distinct_pages = distinct_pages
    out.no_coverage = False
    out.searched_scope = searched_scope or []
    # Set explicitly: an unset attribute on a MagicMock is a truthy
    # Mock, which would take the transient branch of every test here.
    out.transient = transient
    # Same trap, second field: the no-excerpts branch renders
    # ``lib_out.fallback_reason`` and falls back to its fixed sentence
    # only when it is falsy. An unset MagicMock would satisfy that
    # ``or`` and every test here would assert on ``<MagicMock ...>``.
    out.fallback_reason = None
    return out


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


def test_ask_returns_an_inconclusive_envelope_when_librarian_dispatch_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """librarian_agent.run_sync raising → lib_ask() is inconclusive, not empty.

    Reproduces the v0.40 live-test failure: pydantic-ai raises
    ``UsageLimitExceeded("Exceeded maximum output retries (1)")`` when the
    librarian LLM cannot produce a valid ``LibrarianOutput``. The MCP
    ``lib_ask`` tool must not crash.

    The envelope it returns used to be the "No relevant content found in
    library" gap, which named a corpus problem for a model outage --
    on the primary human-facing tool, using the exact string the
    librarian contract designates as the canonical false claim. A
    dispatch that never ran says nothing about the library, so the
    envelope says so instead.
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
    monkeypatch.setattr("lies.agents.librarian.register_librarian_tools", lambda agent, **_kw: None)
    # The synthesizer must NOT be reached on the fallback path.
    synth_called: list[bool] = []
    monkeypatch.setattr(
        synth,
        "synthesizer_agent_run",
        lambda lib_out, q: synth_called.append(True) or MagicMock(),
    )

    with caplog.at_level("WARNING", logger="lies.mcp.synth"):
        out = synth.lib_ask.fn(question="what is pydantic?", tag_expr="c:alpha")

    assert "inconclusive" in out.answer, out.answer
    assert "No relevant content found in library." not in out.answer, (
        "a dispatch failure must not be reported as an empty corpus"
    )
    assert out.synthesis_used is False
    assert out.fallback_used is True
    assert "dispatch failed" in (out.fallback_reason or "").lower()
    assert out.searched_scope == []
    assert synth_called == []
    # The operator should see one warning explaining the fallback fired.
    assert any(
        "librarian_agent_run" in record.getMessage() and "RuntimeError" in record.getMessage()
        for record in caplog.records
    ), f"expected fallback warning, got: {[r.getMessage() for r in caplog.records]}"


def test_librarian_agent_run_names_an_untyped_dispatch_failure_on_the_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The generic arm carries the exception too, so ``or`` has something to fall back from."""
    from lies.mcp import synth

    class _BoomAgent:
        def run_sync(self, user_prompt, *, deps):
            raise RuntimeError("Exceeded maximum output retries (1)")

    monkeypatch.setattr(synth, "librarian_agent", lambda model=None: _BoomAgent())
    monkeypatch.setattr(synth, "_resolve_librarian_model", lambda: "test-model")
    # ``**kwargs`` on the stub: the production call passes
    # ``search_diagnostics=``, so a one-parameter lambda would raise
    # ``TypeError`` and mask what this test is about.
    monkeypatch.setattr("lies.agents.librarian.register_librarian_tools", lambda agent, **_kw: None)

    lib_out = synth.librarian_agent_run(
        synth._build_query_deps(question="q", tag_expr=None, exclude_tags=None)
    )

    assert lib_out.transient is True
    reason = lib_out.fallback_reason or ""
    assert "RuntimeError" in reason, reason
    assert "Exceeded maximum output retries" in reason, reason


def test_a_transient_librarian_bundle_never_becomes_a_corpus_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two flags travel together, and only the right one is read.

    ``no_coverage`` alone cannot express "the dispatch failed": the
    bundle has zero excerpts either way. A consumer that branches on
    ``not excerpts`` first would emit the corpus claim, so
    ``_ask_impl`` checks ``transient`` first, and a bundle that says
    ``transient=True, no_coverage=False`` -- the pair
    ``librarian_agent_run`` now returns -- takes the inconclusive
    branch even though ``no_coverage`` is False.
    """
    from lies.mcp.synth import lib_ask

    lib_out = _fake_librarian_output(
        searched_scope=["alpha"], distinct_pages=0, excerpts=[], transient=True
    )
    lib_out.no_coverage = False
    monkeypatch.setattr("lies.mcp.synth.librarian_agent_run", lambda deps: lib_out)

    synth_called: list[bool] = []
    monkeypatch.setattr(
        "lies.mcp.synth.synthesizer_agent_run",
        lambda lib_out, q: synth_called.append(True) or MagicMock(),
    )

    out = lib_ask.fn(question="what is pydantic?", tag_expr="c:alpha")

    assert "inconclusive" in out.answer
    assert "No relevant content found in library." not in out.answer
    assert synth_called == []


async def test_synthesize_reports_a_transient_digest_as_inconclusive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ArchivistDigest.transient`` finally has a reader.

    The field was set at three sites in ``grounding`` and read
    nowhere in the package, while the 0.46.0 changelog advertised it
    as the way to tell "the daemon failed" from "the corpus has
    nothing". ``synthesize`` is the one registered consumer of a
    digest, so it is where the distinction has to land.
    """
    from lies.mcp import synth
    from lies.mcp.grounding import ArchivistDigest

    digest = ArchivistDigest(
        question="what is pydantic?",
        tag_expr=None,
        exclude_expr=None,
        citations=[],
        no_coverage=False,
        distinct_pages=0,
        transient=True,
    )

    async def _fake_ground(**_kwargs):
        return digest

    monkeypatch.setattr(synth, "ground", _fake_ground)

    out = await synth.synthesize(question="what is pydantic?")

    assert "inconclusive" in out.answer
    assert "No relevant content found in library." not in out.answer
    assert out.synthesis_used is False
    assert "dispatch failed" in (out.fallback_reason or "").lower()


# --- the lock's diagnostics on the *registered* ask path -----------------
#
# ``lib_ask`` is ``_ask_impl``, not ``synthesize``. Round 1 threaded the
# diagnostics through ``ground()`` → ``synthesize`` and proved it there —
# a path whose only production caller is a function nothing else calls,
# and which no registered tool exposes. On the real tool the reason dies
# at the librarian boundary instead: ``search`` fills its envelope's
# ``fallback_reason`` with the lock holder, the wait and the budget, the
# bundle has no slot to receive it, and ``_ask_impl`` renders one fixed
# sentence for every transient failure. The tests below drive
# ``lib_ask.fn`` itself.


def test_lib_ask_renders_the_librarian_bundles_reason_not_a_fixed_sentence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient bundle's own ``fallback_reason`` reaches the envelope."""
    from lies.mcp.synth import lib_ask

    lib_out = _fake_librarian_output(transient=True)
    lib_out.no_coverage = False
    lib_out.fallback_reason = (
        "qmd lock busy: qmd flock contention on /var/lib/lies/qmd.lock: "
        "holder PID 4242, waited 120.0s, max 120.0s"
    )
    monkeypatch.setattr("lies.mcp.synth.librarian_agent_run", lambda deps: lib_out)

    out = lib_ask.fn(question="what is pydantic?", tag_expr="c:alpha")

    reason = out.fallback_reason or ""
    assert "qmd lock busy" in reason, reason
    assert "4242" in reason, f"the holder pid must reach the caller; got {reason!r}"
    assert "120" in reason, f"the budget must reach the caller; got {reason!r}"
    assert "/var/lib/lies/qmd.lock" in reason, (
        f"the lock path must reach the caller; got {reason!r}"
    )
    # Still not a corpus claim.
    assert "inconclusive" in out.answer


def test_lib_ask_keeps_the_fixed_sentence_when_the_bundle_carries_no_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reason-less bundle must not render ``None`` and must not go blank."""
    from lies.mcp.synth import lib_ask

    lib_out = _fake_librarian_output(transient=True)
    lib_out.no_coverage = False
    lib_out.fallback_reason = None
    monkeypatch.setattr("lies.mcp.synth.librarian_agent_run", lambda deps: lib_out)

    out = lib_ask.fn(question="what is pydantic?", tag_expr="c:alpha")

    assert out.fallback_reason == "librarian dispatch failed; no corpus claim made"
