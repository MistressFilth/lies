"""search() runs a single hybrid qmd query across the resolved collection set.

Task 4 moved the read-side search through the qmd access seam
(``lies.qmd.access.daemon_tool``). The CLI path that lived here before
ranked globally and dropped rows whose first ``/`` segment was outside
the resolved scope: a multi-collection ``c:claude_code|c:opencode``
query returned hits from whichever collection ranked highest, and a
collection could be starved to zero rows even when it had matches. The
daemon's ``collections`` parameter is a true push-down and returns
in-scope rows. These tests pin that.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest


def _patch_registry(monkeypatch: pytest.MonkeyPatch, names: list[str]) -> None:
    from lies.library.registry import LibraryCollectionMeta

    metas = [
        LibraryCollectionMeta(
            name=n,
            source_url=f"https://example.test/{n}",
            tags=frozenset({"plugins"}),
            scope_keywords=frozenset(),
        )
        for n in names
    ]
    monkeypatch.setattr("lies.library.registry.library_collection_metas", lambda: iter(metas))
    monkeypatch.setattr("lies.library.registry.library_collection_names", lambda: frozenset(names))


def _bypass_daemon_precheck(monkeypatch: pytest.MonkeyPatch, names: list[str]) -> None:
    """Stub the daemon-side pre-check to claim ``names`` is the qmd view.

    The pre-check in ``_search_impl`` calls the daemon's ``status``
    tool to learn the collection list qmd is *actually* serving
    against. Tests that stub ``_post_query`` (and so do not
    exercise the daemon path) need to bypass that read; this
    stub makes the pre-check pass for the same ``names`` the
    LIES registry returned, so the existing tests can keep
    asserting on the question/scope/envelope without going
    through the seam.
    """
    monkeypatch.setattr(
        "lies.mcp.search._qmd_collection_names_for_check",
        lambda: frozenset(names),
    )


def test_search_passes_question_as_plain_string(monkeypatch: pytest.MonkeyPatch) -> None:
    """search() forwards the question (or the hypothetical) as a plain string.

    Prior to the v0.40.b search bug fix, ``search()`` built a structured
    ``vec: ...\\nlex: ...`` doc to push both legs of qmd's hybrid
    search explicitly. That shape was found to silently lose coverage
    on queries like ``"LSP setup configure language server"`` (the
    single-string form returned 90%-scored hits; the structured form
    returned 0). qmd's hybrid search handles vec+lex internally when
    the question is plain, so ``search()`` now forwards the question
    as a plain string. When ``hypothetical`` is set, it overrides the
    question (HyDE-style substitution).
    """
    from lies.mcp.search import search

    _patch_registry(monkeypatch, ["alpha"])
    _bypass_daemon_precheck(monkeypatch, ["alpha"])

    captured: dict[str, Any] = {}

    def _capture(doc, scope, limit, timeout):
        captured["doc"] = doc
        return []

    monkeypatch.setattr("lies.mcp.search._post_query", _capture)

    search.fn(question="how do I author a plugin?")
    assert captured["doc"] == "how do I author a plugin?"

    search.fn(
        question="how do I author a plugin?",
        hypothetical="A guide that walks through Plugin.define, setup, and the lifecycle hooks.",
    )
    assert (
        captured["doc"]
        == "A guide that walks through Plugin.define, setup, and the lifecycle hooks."
    )


def test_search_resolves_tag_expr_to_collections(monkeypatch: pytest.MonkeyPatch) -> None:
    """tag_expr='c:alpha|c:beta' resolves to {alpha, beta} (sorted)."""
    from lies.mcp.search import _resolve_tag_collections

    _patch_registry(monkeypatch, ["alpha", "beta", "gamma"])

    out = _resolve_tag_collections("c:alpha|c:beta")
    assert out == (["alpha", "beta"], [])


def test_search_unknown_tag_marks_unknown_tags(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unknown tag surfaces in unknown_tags; no daemon call made."""
    from lies.mcp.search import search

    _patch_registry(monkeypatch, ["alpha"])
    _bypass_daemon_precheck(monkeypatch, ["alpha"])

    qmd_called: list[Any] = []
    monkeypatch.setattr(
        "lies.mcp.search._post_query",
        lambda *a, **kw: qmd_called.append((a, kw)) or [],
    )
    result = search.fn(
        question="anything",
        tag_expr="c:nope",
    )
    assert result["unknown_tags"] == ["c:nope"]
    assert result["no_coverage"] is False
    assert result["searched_scope"] == []
    assert result["hits"] == []
    assert qmd_called == [], "must short-circuit on unknown tag"


def test_search_returns_searched_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """search() returns searched_scope = sorted resolved collection names."""
    from lies.mcp.search import search

    _patch_registry(monkeypatch, ["alpha", "beta", "gamma"])
    _bypass_daemon_precheck(monkeypatch, ["alpha", "beta", "gamma"])
    monkeypatch.setattr(
        "lies.mcp.search._post_query",
        lambda doc, scope, limit, timeout: [
            {"path": f"{scope[0]}/page.md", "title": "P", "score": 0.5, "snippet": ""}
        ],
    )
    result = search.fn(question="hi", tag_expr="c:alpha|c:beta")
    assert result["searched_scope"] == ["alpha", "beta"]


def test_search_no_coverage_when_qmd_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """no_coverage=True when corpus has pages but query matched none."""
    from lies.mcp.search import search

    _patch_registry(monkeypatch, ["alpha"])
    _bypass_daemon_precheck(monkeypatch, ["alpha"])
    monkeypatch.setattr(
        "lies.mcp.search._post_query",
        lambda doc, scope, limit, timeout: [],
    )
    result = search.fn(question="anything")
    # corpus has pages but qmd returned 0 hits
    assert result["no_coverage"] is True
    assert result["hits"] == []


def test_search_unexpected_post_query_failure_returns_no_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unexpected error from the seam is folded into the envelope.

    The seam raises two typed errors (``QmdDaemonUnavailable``,
    ``QmdDaemonWedged``) and a process failure is something else.
    A genuine exception that is neither is a defensive
    ``no_coverage=True, fallback_reason=<class>: <msg>`` — the
    caller still gets an envelope, with the class name visible in
    the reason, so an internal bug surfaces to the operator without
    becoming a silent empty result.
    """
    from lies.mcp.search import search

    _patch_registry(monkeypatch, ["alpha"])
    _bypass_daemon_precheck(monkeypatch, ["alpha"])

    def boom(*a: object, **kw: object) -> list[dict[str, Any]]:
        raise RuntimeError("bridge broke")

    monkeypatch.setattr("lies.mcp.search._post_query", boom)
    result = search.fn(question="anything")
    assert result["no_coverage"] is True
    assert "RuntimeError" in (result.get("fallback_reason") or "")
    assert "bridge broke" in (result.get("fallback_reason") or "")


def test_search_hit_is_top_ranked(monkeypatch: pytest.MonkeyPatch) -> None:
    """search() result.hit = first element of hits (top-ranked row)."""
    from lies.mcp.search import search

    _patch_registry(monkeypatch, ["alpha"])
    _bypass_daemon_precheck(monkeypatch, ["alpha"])
    monkeypatch.setattr(
        "lies.mcp.search._post_query",
        lambda doc, scope, limit, timeout: [
            {"path": "alpha/a.md", "title": "A", "score": 0.9, "snippet": "s"},
            {"path": "alpha/b.md", "title": "B", "score": 0.5, "snippet": "s"},
        ],
    )
    result = search.fn(question="hi")
    assert result["hit"]["path"] == "alpha/a.md"
    assert result["hits"][0]["path"] == "alpha/a.md"
    assert result["hits"][1]["path"] == "alpha/b.md"


def test_post_query_delegates_to_daemon_with_library_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_post_query delegates to ``access.daemon_tool`` with the library root.

    Patches ``daemon_tool`` (NOT ``_post_query``) so a regression that
    re-routes the read path through the CLI is caught here. The CLI
    post-filter is gone — see ``test_scoped_search_pushes_the_filter_into_qmd``
    for why the daemon's ``collections`` parameter is the only safe
    push-down — and the daemon's cwd is the library git root, since
    library collections are registered into qmd at that path.
    """
    from lies.mcp.search import _post_query

    seen: dict[str, Any] = {}

    async def _fake_daemon(name: str, arguments: dict[str, Any]) -> Any:
        seen["name"] = name
        seen.update(arguments)
        return SimpleNamespace(
            content=[],
            structured_content={"results": [{"file": "alpha/p.md", "score": 0.7, "title": "P"}]},
            is_error=False,
        )

    monkeypatch.setattr("lies.mcp.search.access", SimpleNamespace(daemon_tool=_fake_daemon))
    monkeypatch.setattr(
        "lies.library.registry.library_git_root",
        lambda: __import__("pathlib").Path("/fake/library/root"),
    )

    out = _post_query("hi\n  ", ["alpha", "beta"], limit=5, timeout=10)

    assert seen["name"] == "query"
    assert seen["collections"] == ["alpha", "beta"]
    assert seen["limit"] == 5
    assert seen["searches"][0]["type"] == "lex"
    assert seen["searches"][1]["type"] == "vec"
    assert out == [{"file": "alpha/p.md", "path": "alpha/p.md", "score": 0.7, "title": "P"}], (
        "rows expose both the daemon's `file` and the existing `path` contract"
    )


def test_post_query_maps_empty_daemon_result_to_empty_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty ``structuredContent.results`` is mapped to ``[]``.

    The daemon returns an empty result for an unknown collection
    (Review Focus #2 — no error, no rows). The short-circuit is in
    the search layer (the registry pre-check), so by the time
    ``_post_query`` runs the daemon has already agreed to scope; the
    empty list here is "the corpus had no in-scope hits".
    """
    from lies.mcp.search import _post_query

    async def _empty(name: str, arguments: dict[str, Any]) -> Any:
        return SimpleNamespace(
            content=[],
            structured_content={"results": []},
            is_error=False,
        )

    monkeypatch.setattr("lies.mcp.search.access", SimpleNamespace(daemon_tool=_empty))
    monkeypatch.setattr(
        "lies.library.registry.library_git_root",
        lambda: __import__("pathlib").Path("/fake/library/root"),
    )

    assert _post_query("anything", ["alpha"], limit=10, timeout=15) == []


def test_post_query_strips_trailing_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    """_post_query strips trailing whitespace for stable wire shape.

    qmd's hybrid search accepts trailing whitespace without
    rejection today, but ``_post_query`` still ``rstrip()``s the
    question so callers that source the question from shapes with
    trailing whitespace (``$()`` substitution, manual concatenation)
    land on the same payload the tests pin.
    """
    from lies.mcp.search import _post_query

    captured: dict[str, Any] = {}

    async def _capture(name: str, arguments: dict[str, Any]) -> Any:
        captured["name"] = name
        captured["searches"] = arguments["searches"]
        return SimpleNamespace(
            content=[],
            structured_content={"results": []},
            is_error=False,
        )

    monkeypatch.setattr("lies.mcp.search.access", SimpleNamespace(daemon_tool=_capture))
    monkeypatch.setattr(
        "lies.library.registry.library_git_root",
        lambda: __import__("pathlib").Path("/fake/library/root"),
    )

    _post_query("hi\n  \n", ["alpha"], limit=10, timeout=15)

    assert captured["name"] == "query"
    assert all(s["query"] == "hi" for s in captured["searches"])
    assert not any(s["query"].endswith("\n") for s in captured["searches"])
    assert not any(s["query"].endswith(" ") for s in captured["searches"])


# ---------------------------------------------------------------------------
# Task 4 — push-down, validation, transience mapping, docid-free hits.
# ---------------------------------------------------------------------------


def _fake_access(
    monkeypatch: pytest.MonkeyPatch,
    rows: list[dict[str, Any]],
    *,
    qmd_collections: list[str] | None = None,
) -> dict[str, Any]:
    """Install an async ``daemon_tool`` fake capturing its arguments.

    Returns the capture dict so the test can assert on the wire
    shape. The fake answers ``status`` (used by the registry
    pre-check) with the given collection list — by default every
    resolved name in the test's own registry, so the pre-check
    passes and the daemon's ``query`` tool is the one being
    exercised. Override ``qmd_collections`` to simulate the
    daemon-side view differing from the LIES registry.
    """
    from lies.qmd.access import QmdDaemonUnavailable, QmdDaemonWedged

    seen: dict[str, Any] = {}

    if qmd_collections is None:
        # Default: the daemon's view matches the LIES registry the
        # test patched in. ``_patch_registry`` always sets
        # ``library_collection_names``; the fakes here cannot read
        # it, so the test must pass an explicit list when it wants
        # the pre-check to fail.
        from lies.library.registry import library_collection_names

        qmd_collections = sorted(library_collection_names())

    async def _fake(name: str, arguments: dict[str, Any]) -> Any:
        seen["name"] = name
        seen.update(arguments)
        if name == "status":
            return SimpleNamespace(
                content=[],
                structured_content={"collections": list(qmd_collections)},
                is_error=False,
            )
        return SimpleNamespace(
            content=[],
            structured_content={"results": list(rows)},
            is_error=False,
        )

    # Carry the typed exceptions on the SimpleNamespace so the
    # ``except`` clauses can evaluate ``access.QmdDaemonUnavailable``
    # and ``access.QmdDaemonWedged`` even when ``access`` is a fake.
    monkeypatch.setattr(
        "lies.mcp.search.access",
        SimpleNamespace(
            daemon_tool=_fake,
            QmdDaemonUnavailable=QmdDaemonUnavailable,
            QmdDaemonWedged=QmdDaemonWedged,
        ),
    )
    return seen


def test_scoped_search_pushes_the_filter_into_qmd(monkeypatch: pytest.MonkeyPatch) -> None:
    """The resolved collection set reaches the daemon as ``collections``.

    The previous CLI path ranked globally and dropped rows in Python,
    which silently starved a multi-collection query to whichever
    collection ranked highest. The daemon's ``collections`` parameter
    is a true push-down and returns in-scope rows. This test asserts
    the argument reaches the daemon in the order the resolver returned
    it (sorted, deterministic), so a regression that filters
    post-hoc cannot pass.

    Review Focus #2/#3 (unknown collection) is checked separately.
    """
    from lies.mcp.search import _search_impl

    _patch_registry(monkeypatch, ["claude_code", "opencode"])
    seen = _fake_access(
        monkeypatch,
        [
            {
                "docid": "#aaaaaa",
                "file": "claude_code/hooks.md",
                "line": 12,
                "score": 0.93,
                "title": "Hooks",
                "snippet": "…",
                "context": None,
            }
        ],
    )

    result = _search_impl("plugin hooks", tag_expr="c:claude_code|c:opencode")

    assert seen["name"] == "query", "search routes through the daemon's query tool"
    assert seen["collections"] == ["claude_code", "opencode"], (
        "filtering after ranking starves a collection to zero rows; "
        f"got {seen.get('collections')!r}"
    )
    assert result["no_coverage"] is False
    assert result["hits"][0]["path"] == "claude_code/hooks.md"
    assert result["hits"][0]["file"] == "claude_code/hooks.md"


def test_daemon_query_passes_limit_to_qmd_and_enforces_it_lies_side(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The envelope's ``limit`` reaches the daemon *and* the LIES-side slice.

    The CLI path's ``--limit`` is inert (qmd parses only ``values.n``
    and overrides with ``results.length``), so the LIES envelope must
    enforce the cap on the daemon path too: a backend that returned
    more rows than asked for would silently ship a wider top-N than
    the caller requested. Belt-and-braces — the daemon honours
    ``limit`` today, the slice is the load-bearing half.
    """
    from lies.mcp.search import _search_impl

    _patch_registry(monkeypatch, ["claude_code"])
    seen = _fake_access(
        monkeypatch,
        [
            {"file": f"claude_code/p{i}.md", "score": 0.9 - i * 0.01, "title": f"P{i}"}
            for i in range(7)
        ],
    )

    result = _search_impl("anything", limit=5)

    # The daemon was asked for 5 rows.
    assert seen["limit"] == 5, (
        f"daemon must receive the envelope's limit; got {seen.get('limit')!r}"
    )
    # The envelope also slices, in case a future backend ignores the
    # wire argument. 7 rows came back, 5 must reach the caller.
    assert len(result["hits"]) == 5, (
        f"a backend that returns more than the limit must be sliced; got {len(result['hits'])} rows"
    )


@pytest.mark.parametrize("registered_empty", [False, True])
def test_a_collection_that_cannot_serve_is_reported_not_scoped_away(
    monkeypatch: pytest.MonkeyPatch, registered_empty: bool
) -> None:
    """An unresolvable collection name is reported, not silently widened.

    The daemon returns an empty result and **no error** for an
    unknown collection (the CLI exits 1 on the same class). Without
    pre-validation, ``search()`` would have read the empty result as
    "the corpus has nothing for this question" — a false claim
    about the corpus, made for a question that named something
    absent.

    The LIES registry may be stale (a name recorded locally that
    qmd never indexed, or has since dropped). The check is against
    qmd's *own* collection list, surfaced via the daemon's
    ``status`` tool. A resolved name absent from that list is
    flagged; a name present (even with zero documents) is left
    alone. The two states get distinct answers — "you named a
    collection that does not exist" vs "your filter matched
    nothing" — which is the load-bearing part of Review Focus
    #2/#3.

    The ``registered_empty`` parameter forces the LIES registry to
    also hold a registered-but-empty collection (a la
    ``wiki_default``). The point of the parameter is to assert the
    unknown-collection path is independent of any other entry in
    the registry: the report on ``c:no_such_collection`` is the
    same whether or not some other collection is empty.
    """
    from lies.mcp.search import _search_impl
    from lies.qmd.access import QmdDaemonUnavailable, QmdDaemonWedged

    names = (
        ["claude_code"] + (["wiki_default"] if registered_empty else []) + ["no_such_collection"]
    )
    _patch_registry(monkeypatch, names)

    # Daemon's view: only ``claude_code`` is indexed. ``wiki_default``
    # (the registered-but-empty collection) and ``no_such_collection``
    # (the LIES-only stale entry) are not in qmd. The fake returns the
    # collection list under ``structuredContent.collections`` because
    # that is the shape the daemon's ``status`` tool surfaces
    # (``server.js:421`` walks ``status.collections``).
    async def _fake(name: str, arguments: dict[str, Any]) -> Any:
        if name == "status":
            return SimpleNamespace(
                content=[],
                structured_content={"collections": ["claude_code"]},
                is_error=False,
            )
        return SimpleNamespace(
            content=[],
            structured_content={"results": []},
            is_error=False,
        )

    # The exception classes are read off the ``access`` module at
    # ``except`` time, so the SimpleNamespace fake must carry both
    # or the first evaluated ``except`` clause raises
    # ``AttributeError`` before the matching one runs.
    monkeypatch.setattr(
        "lies.mcp.search.access",
        SimpleNamespace(
            daemon_tool=_fake,
            QmdDaemonUnavailable=QmdDaemonUnavailable,
            QmdDaemonWedged=QmdDaemonWedged,
        ),
    )

    result = _search_impl("plugin hooks", tag_expr="c:no_such_collection")

    assert result["unknown_tags"] == ["c:no_such_collection"]
    assert result["searched_scope"] == []
    assert result["no_coverage"] is False
    assert result["hits"] == []


def test_daemon_down_is_re_raised_not_swallowed_into_no_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A down daemon is an operator action; it must not be folded into the envelope.

    The previous CLI path mapped an unreachable qmd to
    ``no_coverage=True, fallback_reason='qmd unreachable: …'`` —
    a claim about the corpus that was really a claim about the
    process. Task 2's spec says down fails loudly. The envelope
    re-raises ``QmdDaemonUnavailable`` so the MCP layer (or the
    caller) sees the typed error and the operator message names
    ``lies qmd up`` (not ``lies mcp up``, which is LIES' own
    server).
    """
    from lies.mcp.search import _search_impl
    from lies.qmd.access import QmdDaemonUnavailable, QmdDaemonWedged

    _patch_registry(monkeypatch, ["claude_code"])

    async def _down(name: str, arguments: dict[str, Any]) -> Any:
        if name == "status":
            return SimpleNamespace(
                content=[],
                structured_content={"collections": ["claude_code"]},
                is_error=False,
            )
        raise QmdDaemonUnavailable(
            "qmd daemon is not serving at http://127.0.0.1:8181/mcp. "
            "Start it with 'lies qmd up', or point LIES_QMD_URL at a daemon that is."
        )

    monkeypatch.setattr(
        "lies.mcp.search.access",
        SimpleNamespace(
            daemon_tool=_down,
            QmdDaemonUnavailable=QmdDaemonUnavailable,
            QmdDaemonWedged=QmdDaemonWedged,
        ),
    )

    with pytest.raises(QmdDaemonUnavailable) as excinfo:
        _search_impl("anything")
    assert "lies qmd up" in str(excinfo.value)


def test_daemon_wedged_surfaces_as_transient_with_daemon_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wedged daemon is a slow daemon, not an unreachable one.

    ``no_coverage`` means "this search found nothing", a claim about
    the corpus. A search that timed out learned nothing and has no
    standing to make that claim. The envelope reports
    ``transient=True`` with the daemon's log tail in
    ``fallback_reason`` — the honest "ask again" description, with
    the evidence attached.
    """
    from lies.mcp.search import _search_impl
    from lies.qmd.access import QmdDaemonUnavailable, QmdDaemonWedged

    _patch_registry(monkeypatch, ["claude_code"])

    async def _wedge(name: str, arguments: dict[str, Any]) -> Any:
        if name == "status":
            return SimpleNamespace(
                content=[],
                structured_content={"collections": ["claude_code"]},
                is_error=False,
            )
        raise QmdDaemonWedged(
            "qmd daemon wedged on call to 'query'", last_output="Reranking 40 chunks..."
        )

    monkeypatch.setattr(
        "lies.mcp.search.access",
        SimpleNamespace(
            daemon_tool=_wedge,
            QmdDaemonUnavailable=QmdDaemonUnavailable,
            QmdDaemonWedged=QmdDaemonWedged,
        ),
    )

    result = _search_impl("anything")

    assert result["no_coverage"] is False, (
        "a wedge is a claim about the run, not the corpus; do not "
        "set no_coverage on a process failure"
    )
    assert result["transient"] is True
    assert "Reranking 40 chunks..." in (result["fallback_reason"] or "")
    assert result["hits"] == []


def test_hits_carry_a_path_never_a_docid(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every hit's path is a ``<collection>/<page>`` path, never a docid.

    ``docid`` is ``documents.hash[0:6]`` resolved by
    ``LIKE '<prefix>%' LIMIT 1`` with no ``ORDER BY``; three live
    collisions exist among 5987 active documents. A hit keyed by
    docid silently returns the wrong file on collision. The daemon
    never sends a docid as a path (``file`` is always
    ``displayPath``), so a regression here is one mutation away
    from making hits lookup-unsafe.

    Two assertions:
    - the first segment of every hit's path is in the registry
      (the daemon honoured the ``collections`` push-down)
    - the path is never ``#<docid>`` (the daemon's ``file`` never is)
    """
    from lies.mcp.search import _search_impl

    _patch_registry(monkeypatch, ["claude_code", "opencode"])
    rows = [
        {"file": "claude_code/hooks.md", "score": 0.9, "title": "Hooks"},
        {"file": "opencode/v2/docs/permissions.md", "score": 0.8, "title": "Permissions"},
    ]
    _fake_access(monkeypatch, rows)

    result = _search_impl("anything")

    for hit in result["hits"]:
        assert not hit["path"].startswith("#"), (
            "docid collisions make hits lookup-unsafe; the daemon must "
            "never return a docid where a path belongs"
        )
        first = hit["path"].split("/", 1)[0]
        assert first in {"claude_code", "opencode"}, (
            f"hit path's first segment is not a registered collection: {hit['path']!r}"
        )


def test_searches_emits_lex_and_vec_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare question is forwarded as ``lex`` + ``vec``; ``hyde`` is deferred.

    The plan defers the ``hyde`` decision to Task 6, so the current
    search payload contains exactly two entries with the user's
    question, and the first gets 2x weight (qmd's documented
    behaviour). A regression that adds ``hyde`` here preempts Task
    6's measurement; one that drops ``lex`` starves the BM25 leg.
    """
    from lies.mcp.search import _search_impl

    _patch_registry(monkeypatch, ["claude_code"])
    seen = _fake_access(
        monkeypatch,
        [{"file": "claude_code/hooks.md", "score": 0.5, "title": "Hooks"}],
    )

    _search_impl("plugin hooks")

    types = [s["type"] for s in seen["searches"]]
    assert types == ["lex", "vec"], (
        f"the read-side search payload must be lex+vec only today; got {types!r}"
    )
    queries = [s["query"] for s in seen["searches"]]
    assert queries == ["plugin hooks", "plugin hooks"]
    assert seen["intent"], "intent is required by the daemon's schema"


def test_pre_check_calls_daemon_status_on_every_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pre-check has no cache, so every search pays one daemon ``status`` call.

    Round-2 review found that the previous ``lru_cache(maxsize=1)``
    on ``_qmd_collection_names`` had no production-side clear path,
    so a long-running MCP server that the operator ingested into
    would silently keep its pre-ingest view until restart. Measured
    cost of one daemon ``status`` call against the live daemon
    (2026-10-03, 10 warm samples): p50 41.9 ms. A search runs in
    5.6-6.0 s warm, so the per-search ``status`` call is ~0.7 % of
    the operation and the staleness class is closed in exchange.

    This test pins the property: a cache that survives between
    searches (the prior lru_cache) would let the second ``_search_impl``
    see the first call's value, so daemon ``status`` would be called
    once across two searches. Without the cache, it is called
    twice. The assertion is the bound — a cache that "no test
    exercises" was exactly the field's defect.
    """
    from lies.mcp.search import _search_impl
    from lies.qmd.access import QmdDaemonUnavailable, QmdDaemonWedged

    _patch_registry(monkeypatch, ["claude_code"])

    calls: list[tuple[str, dict[str, Any]]] = []

    async def _fake_daemon_tool(name: str, arguments: dict[str, Any]) -> Any:
        calls.append((name, dict(arguments)))
        if name == "status":
            return SimpleNamespace(
                content=[],
                structured_content={"collections": ["claude_code"]},
                is_error=False,
            )
        return SimpleNamespace(
            content=[],
            structured_content={"results": []},
            is_error=False,
        )

    monkeypatch.setattr(
        "lies.mcp.search.access",
        SimpleNamespace(
            daemon_tool=_fake_daemon_tool,
            QmdDaemonUnavailable=QmdDaemonUnavailable,
            QmdDaemonWedged=QmdDaemonWedged,
        ),
    )

    _search_impl("first question")
    _search_impl("second question")

    status_calls = [c for c in calls if c[0] == "status"]
    assert len(status_calls) == 2, (
        f"every search must read the live daemon status; got "
        f"{len(status_calls)} status calls across 2 searches. "
        f"A cache that survived between searches is exactly the "
        f"staleness class this test exists to prevent."
    )
