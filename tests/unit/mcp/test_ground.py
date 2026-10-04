"""Tests for src/lies/mcp/grounding.py — daemon-based fan-out.

Pins the unscoped-query brick-wall fix: ``ArchivistDigest.no_library``
additive field, ``_fanout_unscoped()`` single-batch daemon
dispatcher, and the ``no_library=True`` fast-path when no library
collections are registered. The fan-out path replaces the F18
librarian LLM round-trip on unscoped queries (which timed out at
~42s / returned 0 citations per session 2505630b's reproduction)
with a single daemon ``query`` against the resolved collection set
via ``lies.qmd.access.daemon_tool`` — the daemon's
``collections`` parameter is a true push-down, so a hybrid search
returns in-scope rows from every named collection.

The previous shape was a per-collection subprocess fan-out with a
consecutive-error counter and a recycle trigger; both are gone
because the seam does its own recycle and the seam's typed errors
(``QmdDaemonUnavailable`` / ``QmdDaemonWedged``) propagate to the
archivist rather than being silently dropped. A new test
(``test_ground_propagates_daemon_unavailable``) pins the
propagation so a future regression that folds a daemon failure
into ``no_coverage=True`` is caught.
"""

from __future__ import annotations

import asyncio
import warnings
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _silence_wiring_skipped_warning() -> None:
    """Silence the ``ground: tool wiring skipped`` warning by default."""
    warnings.filterwarnings(
        "ignore",
        message=r"^ground: tool wiring skipped\b",
        category=UserWarning,
    )


@pytest.fixture(autouse=True)
def _bypass_daemon_precheck(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub ``access.validate_scope`` so the fan-out does not probe status.

    Every fan-out helper here calls ``access.validate_scope`` first
    to drop names the daemon does not serve. The unit tests stub
    ``access.daemon_tool`` directly and want the ``query`` leg
    exercised — the scope pre-check is a real reachability probe and
    must be bypassed the same way the ``search`` suite bypasses it.
    """
    from lies.qmd import access

    async def _passthrough(scope: list[str]) -> tuple[list[str], list[str]]:
        return list(scope), []

    monkeypatch.setattr(access, "validate_scope", _passthrough)


def test_archivist_digest_has_no_library_field_default_false() -> None:
    """`no_library` defaults to False for back-compat with existing call sites."""
    from lies.mcp.grounding import ArchivistDigest

    digest = ArchivistDigest(
        question="q",
        tag_expr=None,
        exclude_expr=None,
        citations=[],
        no_coverage=True,
        distinct_pages=0,
        searched_scope=[],
    )
    assert digest.no_library is False
    assert digest.transient is False


def test_archivist_digest_transient_defaults_false() -> None:
    """``transient`` is additive and defaults to False.

    A clean miss (``no_coverage=True``) and a transient failure
    (``transient=True, no_coverage=False``) are distinct shapes.
    The default-False keeps existing call sites that construct a
    digest by keyword stable — ``ArchivistDigest(...)`` without
    ``transient=`` still means "this was a clean run".
    """
    from lies.mcp.grounding import ArchivistDigest

    digest = ArchivistDigest(
        question="q",
        tag_expr=None,
        exclude_expr=None,
        citations=[],
        no_coverage=False,
        distinct_pages=0,
        searched_scope=[],
    )
    assert digest.transient is False


def test_ground_fanout_dispatch_failure_surfaces_as_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unscoped fan-out dispatch failure is a process claim, not a corpus claim.

    The previous shape folded any fan-out ``Exception`` into
    ``no_coverage=True`` — the same defect the timeout branch
    exists to remove. The fix sets ``transient=True,
    no_coverage=False`` and logs through ``_log.error`` so a
    persistently failing daemon does not go quiet under
    ``warnings.warn``'s once-per-location filter.
    """
    from lies.library import registry as reg_mod
    from lies.library.registry import LibraryCollectionMeta
    from lies.mcp import grounding
    from lies.query import synthesizer as synth_mod

    metas = [LibraryCollectionMeta(name="alpha", tags=())]
    monkeypatch.setattr(reg_mod, "library_collection_metas", lambda: iter(metas))
    monkeypatch.setattr(synth_mod, "_all_collection_names", lambda: ["alpha"])

    async def _boom(*a: object, **kw: object) -> object:
        raise RuntimeError("bridge broke")

    monkeypatch.setattr(grounding, "_fanout_unscoped", _boom)

    digest = asyncio.run(grounding.ground("anything", tag_expr=None))
    assert digest.transient is True
    assert digest.no_coverage is False
    assert digest.citations == []
    assert digest.no_library is False


def test_ground_tagged_fanout_dispatch_failure_surfaces_as_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tagged fan-out dispatch failure is a process claim, not a corpus claim.

    Same shape as the unscoped path. Pinned separately so the
    tagged and untagged fast-paths cannot drift on the failure
    envelope.
    """
    from lies.library import registry as reg_mod
    from lies.library.registry import LibraryCollectionMeta
    from lies.mcp import grounding
    from lies.query import synthesizer as synth_mod
    import lies.mcp.server as server_mod

    metas = [LibraryCollectionMeta(name="alpha", tags=())]
    monkeypatch.setattr(reg_mod, "library_collection_metas", lambda: iter(metas))
    monkeypatch.setattr(synth_mod, "_all_collection_names", lambda: ["alpha"])
    monkeypatch.setattr(
        server_mod,
        "_collect_available_tags_mcp",
        lambda wiki=None: {"alpha", "c:alpha"},
    )

    async def _boom(*a: object, **kw: object) -> object:
        raise RuntimeError("bridge broke")

    monkeypatch.setattr(grounding, "_query_tagged_collections", _boom)

    digest = asyncio.run(grounding.ground("anything", tag_expr="c:alpha"))
    assert digest.transient is True
    assert digest.no_coverage is False


def test_ground_unscoped_uses_fanout(monkeypatch) -> None:
    """Unscoped ground() calls _fanout_unscoped and merges results."""
    from lies.markdown_spans import Span
    from lies.mcp import grounding
    from lies.query import synthesizer as synth_mod

    # Make the registry appear populated so the ``no_library=True``
    # fast-path is bypassed and the fan-out branch fires.
    monkeypatch.setattr(synth_mod, "_all_collection_names", lambda: ["switchyard"])

    @dataclass(frozen=True)
    class _FakeExcerpt:
        collection: str
        slug: str
        title: str
        spans: list = field(default_factory=list)
        source_kind: str = "library"

    fake_excerpts = [
        _FakeExcerpt(
            collection="switchyard",
            slug="switchyard/install.md",
            title="Install",
            spans=[
                Span(
                    heading_path=[],
                    body="install Switchyard binary",
                    code_fence=False,
                    start_line=1,
                )
            ],
        ),
    ]

    async def fake_fanout(question, exclude_expr, top_k):
        assert question == "test question"
        assert exclude_expr is None
        assert top_k == 5
        return grounding._FanoutResult(excerpts=fake_excerpts, searched=["switchyard"], unserved=[])

    monkeypatch.setattr(grounding, "_fanout_unscoped", fake_fanout)

    digest = asyncio.run(grounding.ground("test question", tag_expr=None, top_k=5))
    assert digest.no_coverage is False
    assert digest.no_library is False
    assert len(digest.citations) == 1
    assert digest.citations[0].slug == "switchyard/install.md"


def test_ground_unscoped_no_library_returns_no_library_true(monkeypatch) -> None:
    """When no library collections are registered, ground() returns
    no_library=True and no_coverage=True with empty citations."""
    from lies.mcp import grounding
    from lies.query import synthesizer as synth_mod

    monkeypatch.setattr(synth_mod, "_all_collection_names", lambda: [])

    digest = asyncio.run(grounding.ground("any question", tag_expr=None))
    assert digest.no_library is True
    assert digest.no_coverage is True
    assert digest.citations == []
    assert digest.searched_scope == []


def test_fanout_unscoped_routes_through_the_daemon_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_fanout_unscoped`` issues one daemon ``query`` with the full push-down.

    The previous shape was a per-collection subprocess fan-out that
    paid a model-load cost per call. The seam now serves the
    fan-out: one daemon ``query`` with the resolved collection
    list passed as ``collections=`` and the resolved question as
    ``lex`` + ``vec`` sub-queries. The push-down is exact, so no
    post-filter is needed and one round trip replaces N.

    This test pins the wire shape: the daemon tool is ``query``;
    the collection list is the resolved one; the limit is
    ``top_k``; the search payload is exactly ``[lex, vec]`` (no
    ``hyde``, matching the read-side search contract).
    """
    from lies.library import registry as reg_mod
    from lies.library.registry import LibraryCollectionMeta
    from lies.mcp import grounding
    from lies.qmd import access

    metas = [
        LibraryCollectionMeta(name="alpha", tags=()),
        LibraryCollectionMeta(name="beta", tags=()),
    ]
    monkeypatch.setattr(reg_mod, "library_collection_metas", lambda: iter(metas))

    captured: dict[str, Any] = {}

    async def _fake_daemon(name: str, arguments: dict[str, Any], **kw: Any) -> Any:
        captured["name"] = name
        captured["arguments"] = arguments
        captured["kw"] = kw
        return SimpleNamespace(
            structured_content={
                "results": [
                    {
                        "file": "alpha/page.md",
                        "title": "Page",
                        "score": 0.9,
                        "snippet": "alpha snippet",
                        "line": 1,
                    },
                    {
                        "file": "beta/page.md",
                        "title": "Page B",
                        "score": 0.7,
                        "snippet": "beta snippet",
                        "line": 5,
                    },
                ]
            },
        )

    monkeypatch.setattr(access, "daemon_tool", _fake_daemon)

    excerpts = asyncio.run(grounding._fanout_unscoped("test question", None, top_k=5)).excerpts

    # One call, not N — the push-down is the whole point.
    assert captured["name"] == "query", (
        f"fan-out must reach the daemon via the query tool; got {captured['name']!r}"
    )
    args = captured["arguments"]
    assert sorted(args["collections"]) == ["alpha", "beta"], (
        f"the resolved collection list must reach the daemon as the "
        f"``collections`` push-down; got {args.get('collections')!r}"
    )
    assert args["limit"] == 5, (
        f"top_k must reach the daemon as ``limit=``; got {args.get('limit')!r}"
    )
    assert [s["type"] for s in args["searches"]] == ["lex", "vec"], (
        f"a bare question is one lex + one vec; got {[s['type'] for s in args['searches']]!r}"
    )
    assert all(s["query"] == "test question" for s in args["searches"])
    assert args["intent"], "intent is required by the daemon's schema"

    # The per-call timeout from the shared getter reaches the wire.
    # The seam reads ``_current_timeout()`` and forwards it; the
    # assertion pins the value, not the transport.
    assert captured["kw"].get("timeout") == float(grounding._current_timeout()), (
        f"per-call timeout must reach the daemon; got {captured['kw'].get('timeout')!r}, "
        f"expected {float(grounding._current_timeout())!r}"
    )

    # Both rows surface as PageExcerpts, in score order, with the
    # snippet as a single prose span. ``collection`` is the first
    # ``/`` segment of the path; ``slug`` is the full path.
    assert [e.slug for e in excerpts] == ["alpha/page.md", "beta/page.md"]
    assert [e.collection for e in excerpts] == ["alpha", "beta"]
    assert all(e.source_kind == "library" for e in excerpts)
    assert excerpts[0].spans[0].body == "alpha snippet"
    assert excerpts[1].spans[0].body == "beta snippet"


def test_fanout_unscoped_drops_rows_with_no_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """Daemon rows missing ``file`` are dropped, not converted to bad paths.

    The daemon never sends a row without ``file`` today (Review
    Focus #2 — a row that has no usable ``file`` is a wire-shape
    defect), but a future daemon version that does would slip
    through ``[collection]/[rest]`` parsing and produce
    ``collection=""`` rows. The LIES-side guard is the same one
    ``search`` ships: a missing ``file`` drops the row, so a wire
    defect never lands in the digest as a malformed citation.
    """
    from lies.library import registry as reg_mod
    from lies.library.registry import LibraryCollectionMeta
    from lies.mcp import grounding
    from lies.qmd import access

    metas = [LibraryCollectionMeta(name="alpha", tags=())]
    monkeypatch.setattr(reg_mod, "library_collection_metas", lambda: iter(metas))

    async def _fake_daemon(name: str, arguments: dict[str, Any], **kw: Any) -> Any:
        return SimpleNamespace(
            structured_content={
                "results": [
                    {"file": "", "title": "Bad", "score": 0.9, "snippet": "x"},
                    {"file": "alpha/ok.md", "title": "OK", "score": 0.7, "snippet": "ok"},
                ]
            },
        )

    monkeypatch.setattr(access, "daemon_tool", _fake_daemon)

    excerpts = asyncio.run(grounding._fanout_unscoped("q", None, top_k=5)).excerpts
    assert [e.slug for e in excerpts] == ["alpha/ok.md"]


def test_fanout_unscoped_empty_daemon_result_yields_empty_excerpts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty ``structuredContent.results`` is mapped to ``[]``.

    The daemon answers an unknown collection (or any unscoped
    query that finds nothing) with an empty result and **no
    error**. The fan-out is a no-op on that wire shape — the
    caller reads the empty list as ``no_coverage=True`` through
    the archivist's envelope, which is the honest "the corpus
    had no in-scope hits" answer.
    """
    from lies.library import registry as reg_mod
    from lies.library.registry import LibraryCollectionMeta
    from lies.mcp import grounding
    from lies.qmd import access

    metas = [LibraryCollectionMeta(name="alpha", tags=())]
    monkeypatch.setattr(reg_mod, "library_collection_metas", lambda: iter(metas))

    async def _fake_daemon(name: str, arguments: dict[str, Any], **kw: Any) -> Any:
        return SimpleNamespace(structured_content={"results": []})

    monkeypatch.setattr(access, "daemon_tool", _fake_daemon)

    excerpts = asyncio.run(grounding._fanout_unscoped("anything", None, top_k=5)).excerpts
    assert excerpts == []


def test_fanout_unscoped_enforces_limit_lies_side(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The LIES-side ``rows[:top_k]`` slice is defence in depth.

    The daemon honours ``limit`` today; the slice is what stops a
    future backend that ignores the wire argument from returning a
    wider top-N than asked. Seven rows come back, the cap is five,
    only five reach the caller. The same belt-and-braces lives in
    ``_post_query`` for ``search``.
    """
    from lies.library import registry as reg_mod
    from lies.library.registry import LibraryCollectionMeta
    from lies.mcp import grounding
    from lies.qmd import access

    metas = [LibraryCollectionMeta(name="alpha", tags=())]
    monkeypatch.setattr(reg_mod, "library_collection_metas", lambda: iter(metas))

    async def _fake_daemon(name: str, arguments: dict[str, Any], **kw: Any) -> Any:
        rows = [
            {
                "file": f"alpha/p{i}.md",
                "title": f"P{i}",
                "score": 1.0 - i * 0.01,
                "snippet": f"snippet {i}",
                "line": 1,
            }
            for i in range(7)
        ]
        return SimpleNamespace(structured_content={"results": rows})

    monkeypatch.setattr(access, "daemon_tool", _fake_daemon)

    excerpts = asyncio.run(grounding._fanout_unscoped("anything", None, top_k=5)).excerpts
    assert len(excerpts) == 5, (
        f"a backend that returns more than the limit must be sliced; got {len(excerpts)} rows"
    )


def test_ground_propagates_daemon_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A down daemon is re-raised, not folded into ``no_coverage=True``.

    The previous CLI path's ``except Exception`` swallowed a
    fan-out failure into ``no_coverage=True`` — a claim about the
    corpus, made for a process failure, exactly the class of
    silent failure the timeout-classification branch exists to
    remove. The seam now raises ``QmdDaemonUnavailable`` for a
    down daemon, and ``ground()`` re-raises it so the MCP layer
    (or the Python caller) sees the typed error and the operator
    message names ``lies qmd up``.

    The behaviour pinned here is the *digest shape* on a daemon
    failure: the typed error is what reaches the caller, not a
    silent ``ArchivistDigest(no_coverage=True, citations=[])``
    that the librarian would read as "the corpus has nothing".
    """
    from lies.library import registry as reg_mod
    from lies.library.registry import LibraryCollectionMeta
    from lies.mcp import grounding
    from lies.qmd import access
    from lies.query import synthesizer as synth_mod

    # Both stubs: ``_all_collection_names`` is the archivist's
    # own scope resolver; ``library_collection_metas`` is the
    # registry the fast-path helper reads. The unit-test
    # conftest isolates XDG so the real registry is empty in
    # tests; without this patch the helper short-circuits on an
    # empty list and the daemon is never called.
    metas = [LibraryCollectionMeta(name="alpha", tags=())]
    monkeypatch.setattr(reg_mod, "library_collection_metas", lambda: iter(metas))
    monkeypatch.setattr(synth_mod, "_all_collection_names", lambda: ["alpha"])
    monkeypatch.setattr(
        "lies.mcp.server._collect_available_tags_mcp",
        lambda wiki=None: {"alpha", "c:alpha"},
    )
    monkeypatch.setattr(grounding, "_current_timeout", lambda: 60)

    async def _down(name: str, arguments: dict[str, Any], **kw: Any) -> Any:
        raise access.QmdDaemonUnavailable(
            "qmd daemon is not serving at http://127.0.0.1:8181/mcp. "
            "Start it with 'lies qmd up', or point LIES_QMD_URL at a daemon that is."
        )

    monkeypatch.setattr(access, "daemon_tool", _down)

    with pytest.raises(access.QmdDaemonUnavailable) as excinfo:
        asyncio.run(grounding.ground("anything", tag_expr=None))
    assert "lies qmd up" in str(excinfo.value), (
        f"operator-actionable message must name lies qmd up; got {str(excinfo.value)!r}"
    )


def test_ground_propagates_daemon_wedged(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wedged daemon is re-raised with the seam's ``last_output`` attached.

    A wedge is a process-level claim: the search never finished,
    so the digest has no standing to assert coverage. Folding it
    into ``no_coverage=True`` would be the same false-claim
    class the seam's recycle path was built to prevent. The
    archivist re-raises the typed error so a debugging reader
    can see the daemon's last log tail.
    """
    from lies.library import registry as reg_mod
    from lies.library.registry import LibraryCollectionMeta
    from lies.mcp import grounding
    from lies.qmd import access
    from lies.query import synthesizer as synth_mod

    metas = [LibraryCollectionMeta(name="alpha", tags=())]
    monkeypatch.setattr(reg_mod, "library_collection_metas", lambda: iter(metas))
    monkeypatch.setattr(synth_mod, "_all_collection_names", lambda: ["alpha"])
    monkeypatch.setattr(
        "lies.mcp.server._collect_available_tags_mcp",
        lambda wiki=None: {"alpha", "c:alpha"},
    )
    monkeypatch.setattr(grounding, "_current_timeout", lambda: 60)

    async def _wedge(name: str, arguments: dict[str, Any], **kw: Any) -> Any:
        raise access.QmdDaemonWedged(
            "qmd daemon wedged on call to 'query'",
            last_output="Reranking 40 chunks...",
        )

    monkeypatch.setattr(access, "daemon_tool", _wedge)

    with pytest.raises(access.QmdDaemonWedged) as excinfo:
        asyncio.run(grounding.ground("anything", tag_expr=None))
    assert excinfo.value.last_output == "Reranking 40 chunks...", (
        f"the wedge's last_output must reach the caller; got {excinfo.value.last_output!r}"
    )


def test_ground_tagged_path_propagates_daemon_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tagged fast-path also re-raises ``QmdDaemonUnavailable``.

    A tagged ``ground()`` against a populated library that
    resolves to a non-empty collection set takes the tagged
    fast-path. The same honesty rule applies: a down daemon is
    the operator's action, not a coverage claim. The test pins
    that the tagged path's ``except`` branches mirror the
    unscoped path's.
    """
    from lies.library import registry as reg_mod
    from lies.library.registry import LibraryCollectionMeta
    from lies.mcp import grounding
    from lies.qmd import access
    from lies.query import synthesizer as synth_mod

    metas = [
        LibraryCollectionMeta(name="alpha", tags=()),
        LibraryCollectionMeta(name="beta", tags=()),
    ]
    monkeypatch.setattr(reg_mod, "library_collection_metas", lambda: iter(metas))
    monkeypatch.setattr(synth_mod, "_all_collection_names", lambda: ["alpha", "beta"])
    monkeypatch.setattr(
        "lies.mcp.server._collect_available_tags_mcp",
        lambda wiki=None: {"alpha", "beta", "c:alpha", "c:beta"},
    )

    async def _down(name: str, arguments: dict[str, Any], **kw: Any) -> Any:
        raise access.QmdDaemonUnavailable("daemon is not serving")

    monkeypatch.setattr(access, "daemon_tool", _down)

    with pytest.raises(access.QmdDaemonUnavailable):
        asyncio.run(grounding.ground("anything", tag_expr="c:alpha"))


def _registry_serving(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    """Stand in for a LIES registry holding ``names``.

    Both resolvers, and eagerly. ``_all_collection_names`` feeds the
    scope bookkeeping that runs first -- an empty list there takes the
    ``no_library`` early return before the fan-out is ever reached --
    and ``library_collection_metas`` is what the unscoped fan-out
    dispatches against. Patching only the first leaves the second
    empty under the autouse XDG isolation, and the fan-out silently
    dispatches nothing: the very bug these tests exist to catch,
    arrived at from the other direction.
    """
    metas = [SimpleNamespace(name=n) for n in names]
    monkeypatch.setattr("lies.library.registry.library_collection_names", lambda: list(names))
    monkeypatch.setattr("lies.library.registry.library_collection_metas", lambda: metas)
    monkeypatch.setattr("lies.query.synthesizer._all_collection_names", lambda: list(names))


# --- an unserved collection set is not an empty corpus ---------------
#
# The daemon answers an unknown collection with an empty result and no
# error. `ground` used to drop the unknown set, return `[]`, and let
# `LibrarianOutput(no_coverage=True)` render that as a clean miss --
# so a scope the daemon cannot serve reached the user as "no relevant
# content in the library", naming collections in `searched_scope` that
# were never dispatched. It logged nothing.


def test_ground_reports_an_unserved_collection_set_instead_of_no_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lies.mcp import grounding

    _registry_serving(monkeypatch, "gone_from_qmd")

    async def _validate(scope):
        return [], list(scope)

    monkeypatch.setattr("lies.qmd.access.validate_scope", _validate)

    digest = asyncio.run(grounding.ground("anything", tag_expr=None))

    assert digest.no_coverage is False, (
        "nothing was searched; 'no coverage' is a claim about the corpus and "
        "this fan-out never reached it"
    )
    assert digest.transient is False, (
        "the daemon is serving; this is a scope problem, not a failure"
    )
    assert digest.citations == []
    assert digest.searched_scope == [], "no collection was dispatched, so none may be reported"
    assert digest.unserved_scope == ["gone_from_qmd"]


def test_ground_reports_only_the_collections_it_actually_dispatched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A partially-served scope reports what it sent, and what it could not.

    ``searched_scope`` naming a collection that was filtered out
    before the call tells the reader the daemon searched something it
    did not.
    """
    from lies.mcp import grounding

    _registry_serving(monkeypatch, "alpha", "beta")

    async def _validate(scope):
        return ["alpha"], ["beta"]

    monkeypatch.setattr("lies.qmd.access.validate_scope", _validate)

    captured: dict[str, Any] = {}

    async def _fake_daemon_tool(name, arguments, **kwargs):
        captured.update(arguments)
        return SimpleNamespace(
            structured_content={
                "results": [
                    {
                        "file": "alpha/install.md",
                        "title": "Install",
                        "snippet": "install the binary",
                        "line": 1,
                    }
                ]
            }
        )

    monkeypatch.setattr("lies.qmd.access.daemon_tool", _fake_daemon_tool)

    digest = asyncio.run(grounding.ground("how do i install", tag_expr=None, top_k=3))

    assert captured["collections"] == ["alpha"], "the unserved name must not reach the daemon"
    assert digest.searched_scope == ["alpha"]
    assert digest.unserved_scope == ["beta"]
    assert digest.no_coverage is False
    assert [c.slug for c in digest.citations] == ["alpha/install.md"]


def test_an_unserved_scope_is_logged(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    """The unserved names reach the log, not just the envelope.

    ``AGENTS.md`` says ground "filters silently and logs". The
    silence was the defect: nothing on the operator's side showed
    why a scoped ground found nothing.
    """
    import logging

    from lies.mcp import grounding

    _registry_serving(monkeypatch, "alpha", "gone")

    async def _validate(scope):
        return ["alpha"], ["gone"]

    monkeypatch.setattr("lies.qmd.access.validate_scope", _validate)

    async def _fake_daemon_tool(name, arguments, **kwargs):
        return SimpleNamespace(structured_content={"results": []})

    monkeypatch.setattr("lies.qmd.access.daemon_tool", _fake_daemon_tool)

    with caplog.at_level(logging.WARNING, logger="lies.mcp.grounding"):
        asyncio.run(grounding.ground("anything", tag_expr=None))

    assert "gone" in caplog.text, f"the unserved name must be logged; got {caplog.text!r}"


# --- the exclude side, measured --------------------------------------
#
# `_fanout_collections` opens with `del exclude_expr`, and qmd's
# `collections` push-down is include-only, so a `-tag` in a `ground`
# tail looks inert. It is not: `ground()` resolves the exclude into
# `searched_scope_list` *before* choosing a fast path, and that list
# is what the fan-out dispatches. The parameter is discarded at the
# point where the information has already been applied, and
# `searched_scope` reports the result so a reader can see it applied.
#
# The test below measures that, so the next reader of the `del` can
# check it rather than re-derive it.


def test_an_exclude_only_ground_drops_the_collection_from_the_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lies.mcp import grounding
    from lies.query.tag_expr import parse

    _registry_serving(monkeypatch, "alpha", "beta", "gamma")

    async def _validate(scope):
        return list(scope), []

    monkeypatch.setattr("lies.qmd.access.validate_scope", _validate)

    seen: list[Any] = []

    async def _fake_daemon_tool(name, arguments, **kwargs):
        seen.append(arguments["collections"])
        return SimpleNamespace(structured_content={"results": []})

    monkeypatch.setattr("lies.qmd.access.daemon_tool", _fake_daemon_tool)

    exclude = parse("c:beta")
    digest = asyncio.run(grounding.ground("anything", tag_expr=None, exclude_expr=exclude))

    assert seen == [["alpha", "gamma"]], (
        f"the excluded collection must not reach the daemon; got {seen!r}"
    )
    assert digest.searched_scope == ["alpha", "gamma"], (
        "searched_scope must show the exclusion took effect, not the requested scope"
    )
    assert digest.exclude_expr is exclude, "the digest still reports the expression it was given"
