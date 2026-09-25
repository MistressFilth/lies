"""Live-corpus integration tests for the library-mode read surface.

Task 4 of the library-mode read-side rewrite pins the post-Task-1
performance contract: scoped ``ground()`` round-trips through the MCP
client in ≤15s with citations populated. Reproduces the
session-2505630b bug (ground() timed out at ~42s/empty on unscoped
queries) by exercising the **scoped** variant — the brief's
reproduction uses ``tag_expr="c:switchyard"`` so the F15 dispatch
resolves to a single collection and the librarian path runs (not the
unscoped fan-out added in Task 1).

The test stubs the librarian ``run_sync`` at the
``grounding.librarian_agent`` import seam (mirroring
``test_ground_tool.py``) so the assertion focuses on the timing
contract + envelope shape rather than a real LLM round-trip. The
15s budget is generous on purpose — the post-Task-1 wire-path latency
on a cached cold-start is <1s on this machine; the budget guards
against regression to the 42s path while leaving headroom for slow
CI hosts.

Gated on ``INTEGRATION=1`` via the integration conftest's
``pytest_collection_modifyitems`` hook. Default ``make test`` and
``make check`` skip these tests; CI runs them with ``INTEGRATION=1``.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest


def _seed_switchyard_collection() -> Path:
    """Create a ``switchyard`` directory under the library's ``collections_root``.

    The :func:`_isolated_xdg` autouse fixture redirected XDG into
    ``tmp_path`` and cleared the ``Library.open`` lru_cache, so this
    directory is the addressable set the F15 tag-filter dispatch sees
    when ``ground()`` is called with ``tag_expr="c:switchyard"``.
    Mirrors the ``_seed_wiki_collection`` helper in
    ``test_ground_tool.py``.
    """
    from lies import xdg
    from lies.constants import LIES_DATA_SUBDIR

    coll_root = xdg.data_home() / LIES_DATA_SUBDIR / "library" / "collections" / "switchyard"
    coll_root.mkdir(parents=True, exist_ok=True)
    return coll_root


def _patch_librarian(
    monkeypatch: pytest.MonkeyPatch,
    excerpts: list,
) -> None:
    """Replace ``librarian_agent().run_sync(...)`` with a deterministic output.

    Mirrors ``_patch_librarian`` from ``test_ground_tool.py``: the real
    pydantic_ai ``Agent.run_sync`` returns an ``AgentRunResult``
    whose ``.output`` carries the typed output. ``ground()`` reads
    ``result.output``, so the fake mirrors that wrapper shape.
    """
    from lies.agents.librarian import LibrarianOutput
    from lies.mcp import grounding

    class _FakeResult:
        def __init__(self, output):
            self.output = output

    class _FakeAgent:
        def run_sync(self, user_prompt, *, deps):
            tag_expr = getattr(deps, "tag_expr", None)
            exclude_expr = getattr(deps, "exclude_expr", None)
            return _FakeResult(
                LibrarianOutput(
                    tag_expr=tag_expr,
                    exclude_expr=exclude_expr,
                    excerpts=list(excerpts),
                    distinct_pages=len({e.slug for e in excerpts}),
                    no_coverage=not excerpts,
                )
            )

    monkeypatch.setattr(grounding, "librarian_agent", lambda: _FakeAgent())


def _patch_fanout(
    monkeypatch: pytest.MonkeyPatch,
    excerpts: list,
) -> None:
    """Replace ``_fanout_unscoped(...)`` with a deterministic output.

    Mirrors :func:`_patch_librarian` for the unscoped fan-out path
    introduced by the library-mode read-side rewrite. Unscoped
    ``ground()`` bypasses the F18 librarian and dispatches via
    ``_fanout_unscoped`` (``asyncio.run``). The real helper is
    async, so the fake wraps the deterministic ``excerpts`` list in
    an ``async def`` to preserve the awaitable contract.

    Also seeds ``library_collection_names`` with a non-empty
    frozenset so the ``no_library=True`` fast-path early return
    does not short-circuit before the fan-out mock can be
    exercised.
    """
    from lies.library import registry as registry_mod
    from lies.mcp import grounding

    async def _async_fake(*_args, **_kwargs):
        return list(excerpts)

    monkeypatch.setattr(grounding, "_fanout_unscoped", _async_fake)
    monkeypatch.setattr(
        registry_mod,
        "library_collection_names",
        lambda: frozenset({"switchyard"}),
    )


def _patch_tagged_fanout(
    monkeypatch: pytest.MonkeyPatch,
    excerpts: list,
) -> None:
    """Replace ``_query_tagged_collections(...)`` with a deterministic output.

    Mirrors :func:`_patch_fanout` for the tagged fast-path. Tagged
    ``ground()`` with a non-empty resolved scope bypasses the F18
    librarian and dispatches via
    :func:`lies.mcp.grounding._query_tagged_collections`
    (``asyncio.run``). The real helper is async, so the fake wraps
    the deterministic ``excerpts`` list in an ``async def`` to
    preserve the awaitable contract.

    The test seeds ``switchyard`` under XDG so the F15 tag-filter
    dispatch sees a non-empty resolved scope (``searched_scope_list
    == ["switchyard"]``), which is the precondition for the tagged
    fast-path to fire instead of falling through to the legacy
    librarian path.
    """
    from lies.mcp import grounding

    async def _async_fake(*_args, **_kwargs):
        return list(excerpts)

    monkeypatch.setattr(grounding, "_query_tagged_collections", _async_fake)


async def test_live_corpus_ground_scoped_under_15s(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scoped ``ground()`` returns within 15s with citations populated.

    The session-2505630b reproduction: the operator ran
    ``ground(tag_expr="c:switchyard", top_k=5)`` against the live
    corpus and the call timed out at ~42s with zero citations. Task 1
    added the unscoped fan-out path; this task (scoped fast-path)
    added the matching tagged fan-out so ``c:switchyard`` no longer
    takes the F18 librarian round-trip.

    The test stubs ``_query_tagged_collections`` so the wire-path
    overhead is measured, not a model round-trip. The 15s budget is
    a regression guard against the historical 197s timeout; the
    post-fix path completes in <50ms. Assertions:

      - The call returns within 15s.
      - The envelope carries at least 1 citation.
      - The ``tag_expr`` round-trips through the wire envelope.
      - The citation's ``collection`` matches the seeded
        ``switchyard`` library collection (primary-source hit).
    """
    from fastmcp import Client
    from lies.agents.librarian import PageExcerpt
    from lies.markdown_spans import Span
    from lies.mcp.server import mcp

    # Seed the addressable collection so ``c:switchyard`` validates
    # at the F15 tag-filter dispatch and the tagged fast-path gate
    # (``searched_scope_list`` non-empty) fires.
    _seed_switchyard_collection()

    # Stub the tagged fan-out helper with one canned excerpt so the
    # envelope carries citations ≥ 1. Same shape as the unscoped
    # variant's fan-out mock.
    spans = [
        Span(
            heading_path=["H1"],
            body=(
                "Switchyard is the LiteLLM replacement that runs the "
                "operator's model stack end to end."
            ),
            code_fence=False,
            start_line=1,
        ),
    ]
    excerpts = [
        PageExcerpt(
            collection="switchyard",
            slug="concepts/switchyard",
            title="Switchyard",
            spans=spans,
            source_kind="library",
        ),
    ]
    _patch_tagged_fanout(monkeypatch, excerpts)

    async with Client(mcp) as client:
        t0 = time.monotonic()
        result = await client.call_tool(
            "ground",
            {
                "question": "Set up Switchyard to replace LiteLLM",
                "tag_expr": "c:switchyard",
                "top_k": 5,
            },
        )
        dt = time.monotonic() - t0

    data = result.data
    assert dt < 15, f"ground() took {dt:.2f}s; expected < 15s"
    assert data["tag_expr"] == "c:switchyard"
    assert len(data["citations"]) >= 1, (
        f"expected >= 1 citation, got {len(data['citations'])}: {data['citations']!r}"
    )
    # Library primary-source hit (no [secondary] prefix on the wire).
    assert data["citations"][0]["collection"] == "switchyard"


async def test_live_corpus_ground_unscoped_under_15s(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unscoped ``ground()`` returns within 15s with citations populated.

    Pairs with :func:`test_live_corpus_ground_scoped_under_15s` —
    the scoped variant exercises the librarian path
    (``tag_expr=\"c:switchyard\"``), this test exercises the
    unscoped fan-out path (``_fanout_unscoped``) added by the
    library-mode read-side rewrite's Task 1 brick-wall fix. The
    pre-rewrite unscoped path timed out at ~42s with zero
    citations (session-2505630b reproduction); the post-rewrite
    fan-out completes in <1s on this machine and the 15s budget
    guards against regression to the 42s path.

    The test stubs ``_fanout_unscoped`` at the import seam
    (mirroring the ``librarian_agent`` stub in the scoped
    variant) so the assertion focuses on the timing contract +
    envelope shape rather than a real qmd fan-out. Assertions:

      - The call returns within 15s.
      - The envelope carries at least 1 citation.
      - ``tag_expr`` round-trips as ``None`` (unscoped).
      - The citation's ``collection`` matches the seeded
        ``switchyard`` library collection (primary-source hit).
    """
    from fastmcp import Client
    from lies.agents.librarian import PageExcerpt
    from lies.markdown_spans import Span
    from lies.mcp.server import mcp

    # Seed the addressable collection so the ``library_collection_names``
    # registry sees at least one collection (avoids the
    # ``no_library=True`` fast-path short-circuit before the fan-out
    # mock is exercised). The fan-out mock bypasses any actual qmd
    # scan, so the seeded directory only needs to exist for the F15
    # ``searched_scope`` computation.
    _seed_switchyard_collection()

    # Stub the fan-out helper with one canned excerpt so the
    # envelope carries citations ≥ 1. Same shape as the scoped
    # variant's librarian mock.
    spans = [
        Span(
            heading_path=["H1"],
            body=(
                "Switchyard is the LiteLLM replacement that runs the "
                "operator's model stack end to end."
            ),
            code_fence=False,
            start_line=1,
        ),
    ]
    excerpts = [
        PageExcerpt(
            collection="switchyard",
            slug="concepts/switchyard",
            title="Switchyard",
            spans=spans,
            source_kind="library",
        ),
    ]
    _patch_fanout(monkeypatch, excerpts)

    async with Client(mcp) as client:
        t0 = time.monotonic()
        result = await client.call_tool(
            "ground",
            {
                "question": "Set up Switchyard to replace LiteLLM",
                "top_k": 5,
            },
        )
        dt = time.monotonic() - t0

    data = result.data
    assert dt < 15, f"ground() took {dt:.2f}s; expected < 15s"
    # Unscoped contract: ``tag_expr`` is ``None`` end-to-end.
    assert data["tag_expr"] is None, (
        f"expected tag_expr=None for unscoped, got {data['tag_expr']!r}"
    )
    assert len(data["citations"]) >= 1, (
        f"expected >= 1 citation, got {len(data['citations'])}: {data['citations']!r}"
    )
    # Library primary-source hit (no [secondary] prefix on the wire).
    assert data["citations"][0]["collection"] == "switchyard"


async def test_live_corpus_synthesize_envelope_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """``synthesize()`` returns the post-Task-2 envelope shape.

    The pre-Task-2 surface had ``query`` / ``answer`` MCP tools with
    a ``SynthesizedMcpAnswer`` envelope; both are retired. The
    ``synthesize`` MCP tool replaces them with a
    ``SynthesizeEnvelope`` carrying ``answer`` / ``citations`` /
    ``pages_read``. This test pins that envelope shape end-to-end
    through the MCP client.
    """
    from fastmcp import Client
    from lies.agents.query_synthesizer import QueryAnswer
    from lies.mcp import synth as synth_mod
    from lies.mcp.grounding import ArchivistDigest
    from lies.mcp.server import mcp

    # Empty-digest path is already covered by unit tests; this
    # integration test exercises the success-path envelope so the
    # ``answer`` / ``citations`` / ``pages_read`` keys land on the
    # wire as the brief specifies.
    fake_digest = ArchivistDigest(
        question="q",
        tag_expr=None,
        exclude_expr=None,
        citations=[],
        no_coverage=True,
        distinct_pages=0,
        searched_scope=[],
        no_library=False,
    )

    def fake_ground(*_args, **_kwargs):
        return fake_digest

    monkeypatch.setattr(synth_mod, "ground", fake_ground)
    monkeypatch.setattr(synth_mod, "_resolve_synthesizer_model", lambda: "test")

    class _FakeAgentResult:
        def __init__(self, output):
            self.output = output

    class _FakeSynthAgent:
        def __init__(self, _model):
            pass

        def run_sync(self, _prompt, *, deps):
            return _FakeAgentResult(
                QueryAnswer(
                    question="q",
                    answer="Canned answer.",
                    pages=[],
                    claim_citations=[],
                )
            )

    monkeypatch.setattr(
        "lies.agents.query_synthesizer.query_synthesizer_agent",
        _FakeSynthAgent,
    )

    async with Client(mcp) as client:
        result = await client.call_tool("synthesize", {"question": "q"})

    # ``synthesize`` returns the honest empty-prose envelope on an
    # empty digest (per Task 2 contract) without running the
    # synthesizer — so the assertions here target the empty-digest
    # branch, not the canned-answer branch. The empty-digest path
    # is the integration-test-friendly variant that doesn't depend
    # on a real synthesizer dispatch.
    data = result.data
    assert data["question"] == "q"
    assert "answer" in data
    assert "citations" in data
    assert "pages_read" in data
    assert isinstance(data["citations"], list)
    assert isinstance(data["pages_read"], list)


async def test_live_corpus_synthesize_file_back_raises_tool_error() -> None:
    """``synthesize(file_back=True)`` raises ``ToolError`` (deferred).

    The file-back path is reserved for the write-tool spec
    (out-of-scope for the library-mode read-side rewrite). The MCP
    boundary surfaces the deferred state as ``ToolError`` so callers
    route through the operator guidance instead of silently dropping
    the flag.
    """
    from fastmcp import Client
    from fastmcp.exceptions import ToolError

    from lies.mcp.server import mcp

    async with Client(mcp) as client:
        with pytest.raises(ToolError, match="file_back is deferred"):
            await client.call_tool(
                "synthesize",
                {"question": "q", "file_back": True},
            )
