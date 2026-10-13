"""Live-corpus integration tests for the v0.40 read-side surface."""

from __future__ import annotations

import time
from pathlib import Path

import pytest


def _seed_switchyard_collection() -> Path:
    """Create a ``switchyard`` directory under the library's ``collections_root``.

    The :func:`_isolated_xdg` autouse fixture redirected XDG into
    ``tmp_path`` and cleared the ``Library.open`` lru_cache, so this
    directory is the addressable set the F15 tag-filter dispatch sees
    when ``search()`` is called with ``tag_expr="c:switchyard"``.

    Library collections are global — there is no per-wiki mirror in
    v0.40 — so seeding the library's collections_root is the
    canonical way to register the corpus against the live registry
    for the duration of an integration test.
    """
    from lies import xdg
    from lies.constants import LIES_DATA_SUBDIR

    coll_root = xdg.data_home() / LIES_DATA_SUBDIR / "library" / "collections" / "switchyard"
    coll_root.mkdir(parents=True, exist_ok=True)
    return coll_root


def _stub_post_query(
    monkeypatch: pytest.MonkeyPatch,
    rows: list[dict],
) -> None:
    """Replace ``lies.mcp.search._post_query`` with a deterministic canned list.

    Mirrors the pattern in ``tests/integration/test_corpus_retrieval.py``
    — the production ``_post_query`` shells through qmd's CLI subprocess
    against the library's git root. Stubbing it at this seam avoids a
    real qmd invocation (which would either spin up the daemon or fail
    with a missing-binary error in CI sandboxes). Each row mirrors
    ``qmd_query``'s wire shape (``path`` / ``title`` / ``score`` /
    ``snippet``).

    ``_search_impl`` also validates scope against the daemon's own
    ``status`` before dispatching, which is a real reachability probe.
    That goes with the fan-out, or these tests reach a live daemon —
    which is exactly what the module claims they do not need.
    """
    monkeypatch.setattr(
        "lies.mcp.search._post_query",
        lambda doc, scope, limit, timeout: list(rows),
    )

    async def _served() -> frozenset[str]:
        return frozenset({"switchyard"})

    monkeypatch.setattr("lies.qmd.access.qmd_collection_names", _served)


# ---------------------------------------------------------------------------
# search — single-batch hybrid vec+lex qmd query
# ---------------------------------------------------------------------------


async def test_live_corpus_search_scoped_under_15s(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scoped ``search()`` returns within 15s with hits populated."""
    from fastmcp import Client

    from lies.mcp.server import mcp

    # Seed the addressable collection so ``c:switchyard`` validates
    # at the F15 tag-filter dispatch.
    _seed_switchyard_collection()

    # Stub the qmd dispatch with one canned hit so the envelope
    # carries hits ≥ 1. Same shape as qmd's wire format.
    _stub_post_query(
        monkeypatch,
        rows=[
            {
                "path": "switchyard/concepts/switchyard",
                "title": "Switchyard",
                "score": 0.95,
                "line": 1,
                "snippet": "Switchyard is the LiteLLM replacement that runs the operator's model stack.",
            },
        ],
    )

    async with Client(mcp) as client:
        t0 = time.monotonic()
        result = await client.call_tool(
            "search",
            {
                "question": "Set up Switchyard to replace LiteLLM",
                "tag_expr": "c:switchyard",
            },
        )
        dt = time.monotonic() - t0

    data = result.data
    assert dt < 15, f"search() took {dt:.2f}s; expected < 15s"
    # ``searched_scope`` reflects the resolved include set — the F15
    # tag-filter dispatch surfaces ``c:switchyard`` as the only
    # matched collection against the seeded corpus.
    assert "switchyard" in data["searched_scope"], (
        f"expected switchyard in searched_scope; got {data['searched_scope']!r}"
    )
    assert len(data["hits"]) >= 1, f"expected >= 1 hit, got {len(data['hits'])}: {data['hits']!r}"
    assert "switchyard" in data["hits"][0]["path"]


async def test_live_corpus_search_unscoped_under_15s(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unscoped ``search()`` returns within 15s with hits populated."""
    from fastmcp import Client

    from lies.mcp.server import mcp

    # Seed the addressable collection so the unscoped fan-out sees
    # at least one collection registered. The fan-out walks the
    # library's collection set; without a seed the registry reports
    # zero collections and ``search()`` short-circuits with
    # ``no_coverage=True`` and zero hits.
    _seed_switchyard_collection()

    # Stub the qmd dispatch with one canned hit so the envelope
    # carries hits ≥ 1.
    _stub_post_query(
        monkeypatch,
        rows=[
            {
                "path": "switchyard/concepts/switchyard",
                "title": "Switchyard",
                "score": 0.95,
                "line": 1,
                "snippet": "Switchyard is the LiteLLM replacement that runs the operator's model stack.",
            },
        ],
    )

    async with Client(mcp) as client:
        t0 = time.monotonic()
        result = await client.call_tool(
            "search",
            {
                "question": "Set up Switchyard to replace LiteLLM",
            },
        )
        dt = time.monotonic() - t0

    data = result.data
    assert dt < 15, f"search() took {dt:.2f}s; expected < 15s"
    # Unscoped contract: ``searched_scope`` = every registered
    # collection (sorted). The seeded switchyard collection must
    # surface in the scope since the unscoped fan-out walks the
    # full library.
    assert "switchyard" in data["searched_scope"], (
        f"unscoped searched_scope must include switchyard; got {data['searched_scope']!r}"
    )
    assert len(data["hits"]) >= 1, f"expected >= 1 hit, got {len(data['hits'])}: {data['hits']!r}"
    assert "switchyard" in data["hits"][0]["path"]


# ---------------------------------------------------------------------------
# lib_ask — librarian + synthesizer orchestrator
# ---------------------------------------------------------------------------


async def test_live_corpus_ask_envelope_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """``lib_ask()`` returns the post-v0.40 envelope shape."""
    from fastmcp import Client

    # Stub the librarian with a canned output (zero excerpts) so the
    # envelope falls onto the empty-prose branch — the
    # integration-test-friendly variant that doesn't depend on a
    # real synthesizer dispatch. The branch is the same as an
    # untagged-scoped search against an empty library.
    from lies.agents.librarian import LibrarianOutput
    from lies.mcp.server import mcp

    lib_out = LibrarianOutput(
        tag_expr="c:switchyard",
        exclude_expr=None,
        excerpts=[],
        distinct_pages=0,
        no_coverage=True,
        searched_scope=["switchyard"],
    )
    monkeypatch.setattr("lies.mcp.synth.librarian_agent_run", lambda deps: lib_out)

    # Spy on the synthesizer — if the empty-prose branch fires, the
    # synthesizer must NOT run.
    synth_called: list[object] = []

    def _spy_synth(lib_out_arg: object, question: str) -> object:
        synth_called.append((lib_out_arg, question))
        return None

    monkeypatch.setattr("lies.mcp.synth.synthesizer_agent_run", _spy_synth)

    async with Client(mcp) as client:
        result = await client.call_tool("lib_ask", {"question": "anything"})

    data = result.data
    # ``lib_ask`` returns a typed ``SynthesizeEnvelope``; FastMCP 4.x
    # validates the result through a generated pydantic Root model,
    # so attribute access (not dict access) lands on each field.
    assert data.question == "anything"
    assert data.answer == "No relevant content found in library."
    assert isinstance(data.citations, list)
    assert isinstance(data.pages_read, list)
    assert data.fallback_used is True
    assert data.synthesis_used is False
    assert data.searched_scope == ["switchyard"]
    # Empty-prose branch: synthesizer must NOT run.
    assert synth_called == [], (
        f"synthesizer must not run on empty librarian output; got {synth_called!r}"
    )


async def test_live_corpus_ask_file_back_raises_tool_error() -> None:
    """``lib_ask(file_back=True)`` raises ``ToolError`` (deferred)."""
    from fastmcp import Client
    from fastmcp.exceptions import ToolError

    from lies.mcp.server import mcp

    async with Client(mcp) as client:
        with pytest.raises(ToolError, match="file_back deferred"):
            await client.call_tool(
                "lib_ask",
                {"question": "q", "file_back": True},
            )
