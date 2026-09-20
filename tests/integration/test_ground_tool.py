"""Integration tests for the ``ground`` MCP tool.

Three round-trips through a real FastMCP ``Client``:

1. ``ground`` is registered as a tool on the ``mcp`` server instance.
2. Calling ``ground`` with a question returns a JSON-serialized
   :class:`ArchivistDigest` carrying every documented field.
3. Calling ``ground`` with ``tag_expr="wiki"`` survives tag-filter
   dispatch (a ``wiki`` library collection is seeded) and surfaces
   only up to ``top_k`` citations.

The librarian agent's ``run_sync`` is mocked at the
``grounding.librarian_agent`` import seam so the test does not
require a real model round-trip. The library's ``collections_root``
is seeded with a ``wiki`` directory so the F15 tag-filter dispatch
in :func:`ground` resolves ``+wiki`` without raising
:class:`ArchivistCoverageError`.

Gated on ``INTEGRATION=1`` per the integration conftest; the
integration workflow in ``.github/workflows/`` runs with that env
set. Outside that env, every test in this file skips via the
conftest's ``pytest_collection_modifyitems`` hook.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastmcp import Client

from lies import xdg
from lies.constants import LIES_DATA_SUBDIR
from lies.agents.librarian import LibrarianOutput, PageExcerpt
from lies.markdown_spans import Span
from lies.mcp import grounding
from lies.mcp.server import mcp


pytestmark = pytest.mark.skipif(
    os.environ.get("INTEGRATION") != "1",
    reason="gated on INTEGRATION=1 (real qmd daemon + fixture wiki)",
)


def _seed_wiki_collection() -> Path:
    """Create a ``wiki`` directory under the library's ``collections_root``.

    The :func:`_isolated_xdg` autouse fixture has redirected XDG into
    ``tmp_path`` and cleared the ``Library.open`` lru_cache, so this
    directory is the addressable set the F15 tag-filter dispatch sees
    when ``ground()`` is called with ``tag_expr="wiki"``.
    """
    coll_root = xdg.data_home() / LIES_DATA_SUBDIR / "library" / "collections" / "wiki"
    coll_root.mkdir(parents=True, exist_ok=True)
    return coll_root


def _patch_librarian(
    monkeypatch: pytest.MonkeyPatch,
    excerpts: list[PageExcerpt],
) -> None:
    """Replace ``librarian_agent().run_sync(...)`` with a deterministic output.

    Mirrors the ``_patch_librarian`` helper in
    ``tests/unit/mcp/test_grounding.py``: the real pydantic_ai
    ``Agent.run_sync`` returns an ``AgentRunResult`` whose ``.output``
    carries the typed output. ``ground()`` reads ``result.output``,
    so the fake mirrors that wrapper shape.
    """

    class _FakeResult:
        def __init__(self, output: object) -> None:
            self.output = output

    class _FakeAgent:
        def run_sync(self, prompt: object, deps: object = None) -> _FakeResult:
            tag_expr = getattr(deps, "tag_expr", None)
            exclude_tags = list(getattr(deps, "exclude_tags", []) or [])
            return _FakeResult(
                LibrarianOutput(
                    tag_expr=tag_expr,
                    exclude_tags=exclude_tags,
                    excerpts=list(excerpts),
                    distinct_pages=len({e.slug for e in excerpts}),
                )
            )

    monkeypatch.setattr(grounding, "librarian_agent", lambda: _FakeAgent())


async def test_ground_tool_registered_with_mcp_server() -> None:
    """The MCP server exposes the ``ground`` tool."""
    async with Client(mcp) as client:
        tools = await client.list_tools()
        names = {t.name for t in tools}
        assert "ground" in names


async def test_ground_tool_returns_archivist_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: MCP tool call returns a digest with the expected shape."""
    spans = [
        Span(
            heading_path=["H1"],
            body="Pydantic is a Python data validation library with hooks.",
            code_fence=False,
            start_line=1,
        ),
    ]
    excerpts = [
        PageExcerpt(collection="wiki", slug="concepts/pydantic", title="Pydantic", spans=spans),
    ]
    _patch_librarian(monkeypatch, excerpts)

    async with Client(mcp) as client:
        result = await client.call_tool(
            "ground",
            {"question": "what is the pydantic hook?"},
        )

    # ``result.data`` is the parsed structured content (a dict for
    # non-pydantic returns). FastMCP 4.x coerces the function's
    # ``dict`` return through the auto-generated output schema.
    data = result.data
    assert "citations" in data
    assert "question" in data
    assert "no_coverage" in data
    assert "tag_expr" in data
    assert "exclude_tags" in data
    assert "distinct_pages" in data
    assert data["question"] == "what is the pydantic hook?"
    assert data["tag_expr"] is None
    assert data["exclude_tags"] == []
    assert data["no_coverage"] is False
    assert data["distinct_pages"] == 1
    assert len(data["citations"]) == 1
    assert data["citations"][0]["slug"] == "concepts/pydantic"
    assert data["citations"][0]["collection"] == "wiki"


async def test_ground_tool_with_tag_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tag-filter dispatch surfaces only matching collections.

    Seed a ``wiki`` collection directory under the library's
    ``collections_root`` so ``+wiki`` is a known include token at
    the F15 dispatch layer. With the librarian agent mocked to
    return zero excerpts the citation list is empty (still ``<= 2``),
    but the call must not raise :class:`ArchivistCoverageError`.
    """
    _seed_wiki_collection()
    _patch_librarian(monkeypatch, excerpts=[])

    async with Client(mcp) as client:
        result = await client.call_tool(
            "ground",
            {"question": "any question", "tag_expr": "wiki", "top_k": 2},
        )

    data = result.data
    assert isinstance(data["citations"], list)
    assert len(data["citations"]) <= 2
    for c in data["citations"]:
        assert "snippet" in c
        assert len(c["snippet"]) <= 200
        assert "slug" in c
        assert "title" in c
        assert "collection" in c
    # ``tag_expr`` round-trips through the tool; the seed of an empty
    # excerpt list still surfaces ``no_coverage=False`` (librarian
    # dispatched, classification verdict not "no coverage").
    assert data["tag_expr"] == "wiki"
    assert data["no_coverage"] is False
