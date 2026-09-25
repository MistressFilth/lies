"""Integration tests for the ``ground`` MCP tool.

Four round-trips through a real FastMCP ``Client``:

1. ``ground`` is registered as a tool on the ``mcp`` server instance.
2. Calling ``ground`` with a question returns a JSON-serialized
   :class:`ArchivistDigest` carrying every documented field.
3. Calling ``ground`` with ``tag_expr="wiki"`` survives tag-filter
   dispatch (a ``wiki`` library collection is seeded) and surfaces
   only up to ``top_k`` citations.
4. ``ground`` actually wires ``wiki_search`` / ``wiki_read`` /
   ``wiki_catalog`` onto the librarian agent before dispatch — a
   regression pin for the F19 Critical bug at
   ``src/lies/mcp/grounding.py:228`` (Fix Critical; pre-fix the
   ``ground`` tool emitted an empty digest in production).

Tests 2 and 3 mock the librarian agent's ``run_sync`` at the
``grounding.librarian_agent`` import seam so they do not require a
real model round-trip. Test 4 uses pydantic-ai's ``TestModel`` so
the wiring assertions land on a real :class:`Agent` instance. The
library's ``collections_root`` is seeded with a ``wiki`` directory
in test 3 so the F15 tag-filter dispatch in :func:`ground` resolves
``+wiki`` without raising :class:`ArchivistCoverageError`.

Gated on ``INTEGRATION=1`` per the integration conftest; the
integration workflow in ``.github/workflows/`` runs with that env
set. Outside that env, every test in this file skips via the
conftest's ``pytest_collection_modifyitems`` hook.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp import Client

from lies import xdg
from lies.constants import LIES_DATA_SUBDIR
from lies.agents.librarian import LibrarianOutput, PageExcerpt
from lies.markdown_spans import Span
from lies.mcp import grounding
from lies.mcp.server import mcp


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
        def run_sync(self, user_prompt: object, *, deps: object) -> _FakeResult:
            tag_expr = getattr(deps, "tag_expr", None)
            # Task 3 / f15-exclude-compound: ``exclude_expr`` is now a
            # ``TagExpr | None`` AST the F18 librarian consumes — the
            # historical ``exclude_tags: list[str]`` flat-list field
            # on ``LibrarianDeps`` was retired along with the matching
            # ``LibrarianOutput.exclude_tags`` field.
            exclude_expr = getattr(deps, "exclude_expr", None)
            # F18 Task 3 pin: when the librarian's mock returns zero
            # excerpts, classify the dispatch as a tag-filter scope
            # miss (``no_coverage=True``) so the FastMCP surfacing
            # pin in ``test_ground_tool_with_tag_filter`` exercises
            # the post-Task-2 ``LibrarianOutput.no_coverage`` →
            # ``ArchivistDigest.no_coverage`` plumbing. A real
            # ``WikiSearchResult`` would set this flag from the
            # corpus-page-count + zero-hits branch; the mock
            # short-circuits that wiring.
            return _FakeResult(
                LibrarianOutput(
                    tag_expr=tag_expr,
                    exclude_expr=exclude_expr,
                    excerpts=list(excerpts),
                    distinct_pages=len({e.slug for e in excerpts}),
                    no_coverage=not excerpts,
                )
            )

    monkeypatch.setattr(grounding, "librarian_agent", lambda model=None: _FakeAgent())


def _patch_fanout(
    monkeypatch: pytest.MonkeyPatch,
    excerpts: list[PageExcerpt],
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
        lambda: frozenset({"wiki"}),
    )


async def test_ground_tool_registered_with_mcp_server() -> None:
    """The MCP server exposes the ``ground`` tool."""
    async with Client(mcp) as client:
        tools = await client.list_tools()
        names = {t.name for t in tools}
        assert "ground" in names


async def test_ground_tool_returns_archivist_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: MCP tool call returns a digest with the expected shape.

    Library-mode rewrite migration: unscoped ``ground()`` no longer
    dispatches through the F18 librarian (Task 1 brick-wall fix
    replaced that path with a direct qmd fan-out across registered
    library collections). Patch ``_fanout_unscoped`` directly with
    the deterministic excerpts so the MCP wire envelope assertion
    still pins the post-rewrite shape end-to-end.
    """
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
    _patch_fanout(monkeypatch, excerpts)

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
    # Task 3 / f15-exclude-compound: ``ArchivistDigest.exclude_expr``
    # replaces the flat-list ``exclude_tags`` field. The MCP boundary
    # translates the wire-level ``exclude_tags: list[str]`` to the
    # ``TagExpr | None`` AST (Task 4 / f15-exclude-compound), which
    # ``ArchivistDigest`` then surfaces as ``exclude_expr``.
    assert "exclude_expr" in data
    assert "distinct_pages" in data
    assert data["question"] == "what is the pydantic hook?"
    assert data["tag_expr"] is None
    assert data["exclude_expr"] is None
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
    # ``tag_expr`` round-trips through the tool. F18 Task 3 pin: when
    # the librarian's tag filter excludes every hit on a populated
    # wiki, ``LibrarianOutput.no_coverage`` surfaces through the
    # MCP tool as ``no_coverage=True`` (post-Task-2 ``ground()``
    # reads the field directly from the bundle — the catalog probe
    # was retired). The mocked librarian's empty-excerpt path
    # classifies the dispatch as a scope miss, so the surface
    # must carry ``no_coverage=True``.
    assert data["tag_expr"] == "wiki"
    if len(data["citations"]) == 0:
        assert data["no_coverage"] is True, (
            f"tag filter excluded all hits; expected no_coverage=True, got {data['no_coverage']}"
        )


async def test_ground_tool_compound_exclude_tags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ground(exclude_tags=["c:wiki&c:other"])`` parses a compound exclude.

    Task 4 / f15-exclude-compound: the ``mcp_ground`` boundary parses
    each ``exclude_tags[i]`` as a full F15 expression via
    :func:`lies.query.tag_expr.parse` and validates it against
    :func:`_collect_available_tags_mcp` — same surface as the
    ``query`` / ``answer`` path. The pre-Task-4 boundary only built
    a single-atom ``Include`` from ``check_qualifier``; a compound
    ``c:foo&c:bar`` would have arrived at the librarian as
    ``Include("foo&c:bar", "c")`` with a literal ``&c:bar`` body.
    """
    _seed_wiki_collection()
    _patch_librarian(monkeypatch, excerpts=[])

    async with Client(mcp) as client:
        result = await client.call_tool(
            "ground",
            {"question": "any question", "exclude_tags": ["c:wiki&c:wiki"]},
        )

    # Compound exclude parsed without raising — the boundary validated
    # every atom against the registered set (the seeded ``wiki`` col is
    # the only addressable collection under the hermetic XDG).
    assert result.data["no_coverage"] is True
    assert result.data["exclude_expr"] is not None
    # The MCP envelope serializes ``Include`` / ``And`` / ``Or`` trees
    # via :func:`dataclasses.asdict` — nested ``{"left", "right"}``
    # dicts for ``And`` / ``Or`` and a ``{"tag", "qualifier"}`` dict
    # for ``Include``. The wire shape preserves the compound structure
    # so downstream consumers can introspect it without re-parsing
    # the boundary expression.
    data = result.data
    assert data["exclude_expr"] == {
        "left": {"tag": "wiki", "qualifier": "c"},
        "right": {"tag": "wiki", "qualifier": "c"},
    }


async def test_ground_tool_exclude_tags_bad_grammar_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ground(exclude_tags=["c:foo&"])`` raises ``ToolError`` at the boundary.

    A dangling ``&`` is a parse error in the F15 grammar; the MCP
    boundary surfaces it as ``ToolError("invalid tag expression: ...")`
    rather than letting the malformed AST reach the librarian. The
    FastMCP client surfaces ``ToolError`` from ``call_tool`` directly
    by default (``raise_on_error=True``), so the test uses
    :func:`pytest.raises` rather than asserting on
    ``result.is_error``.
    """
    from fastmcp.exceptions import ToolError
    from lies.query.tag_expr import Include  # noqa: F401  (import probe)

    _seed_wiki_collection()
    _patch_librarian(monkeypatch, excerpts=[])

    async with Client(mcp) as client:
        with pytest.raises(ToolError, match="invalid tag expression"):
            await client.call_tool(
                "ground",
                {"question": "any question", "exclude_tags": ["c:wiki&"]},
            )


async def test_ground_tool_wires_librarian_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end wiring: ``ground`` calls ``register_librarian_tools`` before dispatch.

    Pins Fix Critical: the F19 ground tool MUST wire
    ``wiki_search`` / ``wiki_read`` / ``wiki_catalog`` on the
    librarian agent before invoking ``run_sync``. The pre-fix
    surface called ``librarian_agent().run_sync(...)`` directly,
    producing an agent with no tools and an empty digest in
    production.

    Drives a real FastMCP ``Client`` against ``mcp`` with:
      - a registered wiki so ``resolve_wiki()`` resolves
      - ``register_librarian_tools`` patched to a spy that captures
        the agent's tool names before / after wiring
      - ``WikiMemoryService`` patched to a stub that returns empty
        results — ``TestModel`` may invoke the tools, but the
        assertions pin wiring (not retrieval); a real
        ``WikiMemoryService`` would force qmd state we don't need
      - ``grounding.librarian_agent`` replaced with a real
        ``librarian_agent(model="test")`` factory so the wiring
        code's call lands on a real pydantic-ai agent

    The test asserts the wiring ran (3 tools registered) and the
    digest shape round-trips through the MCP tool envelope.
    """
    from lies.agents import librarian as librarian_mod
    from lies.mcp import grounding
    from lies.mcp import resolution as resolution_mod

    # Set up a wiki at the XDG redirect path so ``resolve_wiki()``
    # finds it via ``Wiki.require``. The autouse ``_isolated_xdg``
    # fixture in ``tests/conftest.py`` redirected XDG into
    # ``tmp_path``; the catalog probe below relies on a registered
    # wiki so we seed one here.
    from tests.conftest import make_wiki

    wiki_root = xdg.data_home() / LIES_DATA_SUBDIR / "default"
    wiki_root.mkdir(parents=True, exist_ok=True)
    (wiki_root / "wiki").mkdir(parents=True, exist_ok=True)
    # ``Wiki.require`` only checks ``data_root.exists()``; the
    # catalog seed reads from disk so one markdown page is enough.
    (wiki_root / "wiki" / "index.md").write_text("# Index\n", encoding="utf-8")
    monkeypatch.setenv("LIES_WIKI_NAME", "default")
    wiki = make_wiki(name="default", data_root=wiki_root)

    monkeypatch.setattr(resolution_mod, "resolve_wiki", lambda name=None: wiki)

    # ``WikiMemoryService.search`` is the wired tool's entry point
    # for ``wiki_search``; ``TestModel`` may invoke it. Patch it to
    # a stub that returns an empty result envelope so the tool call
    # completes without touching qmd. The wiring assertions don't
    # require any actual retrieval — only that the agent IS wired.
    from lies.memory import service as service_mod
    from lies.memory.models import WikiSearchResult

    def stub_search(self: object, question: str, **kwargs: object) -> WikiSearchResult:
        return WikiSearchResult(
            query=question,
            pages=[],
            truncated=False,
            fallback_used=False,
            fallback_reason="stub",
        )

    monkeypatch.setattr(service_mod.WikiMemoryService, "search", stub_search)

    def stub_read(self: object, page_ids: list[str]) -> dict[str, str]:
        # Empty body envelope; ``TestModel`` may invoke this with
        # arbitrary page_ids.
        return {pid: "" for pid in page_ids}

    monkeypatch.setattr(service_mod.WikiMemoryService, "read", stub_read)

    captured: dict[str, object] = {}
    original_register = librarian_mod.register_librarian_tools

    def spy(agent: object, *, wiki: object, memory_service: object) -> None:
        """Wrap ``register_librarian_tools`` so the test can inspect tool names.

        The spy records the agent's tool names BEFORE delegating to
        the real helper, then again AFTER, so the test pins both the
        bare-agent empty-tools state and the wired-agent trio. The
        real helper runs so the agent is genuinely wired (a fake
        spy that skips registration would let the assertions pass
        against an unwired agent and miss the regression).
        """
        if "agent_before" not in captured:
            captured["agent_before"] = _registered_tool_names(agent)
            captured["agent"] = agent
        original_register(agent, wiki=wiki, memory_service=memory_service)
        captured["agent_after"] = _registered_tool_names(agent)

    monkeypatch.setattr(librarian_mod, "register_librarian_tools", spy)
    # Build a real librarian agent (with the ``test`` model so the
    # factory doesn't try to instantiate an Anthropic provider) —
    # the spy captures the wiring on the agent this returns.
    monkeypatch.setattr(
        grounding,
        "librarian_agent",
        lambda model="test": librarian_mod.librarian_agent(model="test"),
    )

    # Library-mode rewrite migration: unscoped ``ground()`` bypasses
    # the F18 librarian entirely (Task 1 brick-wall fix), so wiring
    # never fires on the unscoped path. Force the librarian path by
    # passing ``tag_expr="wiki"``; the seeded ``wiki`` library
    # collection (created via the ``_seed_wiki_collection`` helper
    # above) satisfies the F15 tag-filter dispatch. The wiring spy
    # then records the canonical wiring call against the active
    # wiki's :class:`WikiMemoryService`.
    #
    # Post-rewrite revision: passing ``tag_expr="wiki"`` alone lets
    # ``_collections_matching`` resolve ``wiki`` to the seeded
    # collection and the tagged fast-path skips the wiring block.
    # Pair the include with an exclude that drops the matched
    # collection (``exclude_tags=["c:wiki"]``), so
    # ``searched_scope_list`` resolves to ``[]`` and the legacy F18
    # librarian branch fires — the regression contract this test
    # pins lives on that branch only.
    _seed_wiki_collection()

    async with Client(mcp) as client:
        result = await client.call_tool(
            "ground",
            {
                "question": "what is pydantic?",
                "tag_expr": "wiki",
                "exclude_tags": ["c:wiki"],
            },
        )

    # The bare agent had zero tools; wiring added the F18 trio.
    assert captured["agent_before"] == []
    assert "wiki_search" in captured["agent_after"]
    assert "wiki_read" in captured["agent_after"]
    assert "wiki_catalog" in captured["agent_after"]
    # The digest envelope round-trips through the MCP tool wire.
    data = result.data
    assert "citations" in data
    assert data["question"] == "what is pydantic?"
    # ``no_coverage`` is computed against the corpus — the seeded
    # wiki has one ``index.md`` (a system file excluded from the
    # catalog), so the catalog has 0 wiki-section rows and
    # ``no_coverage=False``. The exact shape depends on
    # ``TestModel``'s canned output, but the envelope is intact.
    assert "no_coverage" in data
    assert "distinct_pages" in data


def _registered_tool_names(agent: object) -> list[str]:
    """Collect the names of every tool registered on ``agent``.

    Mirrors the helper in ``tests/unit/memory/test_tools.py`` —
    pydantic-ai exposes the function toolset via ``agent.toolsets``;
    iterating each toolset's ``tools`` mapping yields the registered
    tool names.
    """
    names: list[str] = []
    for toolset in getattr(agent, "toolsets", []):
        names.extend(getattr(toolset, "tools", {}).keys())
    return sorted(set(names))
