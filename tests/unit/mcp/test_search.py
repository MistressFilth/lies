"""Search() runs a single hybrid qmd query across the resolved collection set."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest


def _patch_registry(
    monkeypatch: pytest.MonkeyPatch,
    names: list[str],
    tags_by_name: dict[str, frozenset[str]] | None = None,
) -> None:
    """Stub the library registry with ``names``, each tagged ``plugins``.

    ``library_collection_tags`` is patched alongside the name and meta
    accessors because the shared filter vocabulary is built from all
    three: ``_collect_available_tags_mcp`` unions the collection names,
    their ``c:``-prefixed spellings, and every registered tag with its
    ``t:``-prefixed spelling. A tag vocabulary that came from the real
    registry instead of the fixture's would leave ``resolve`` rejecting
    the fixture's own tag.
    """
    from lies.library.registry import LibraryCollectionMeta

    if tags_by_name is None:
        tags_by_name = {n: frozenset({"plugins"}) for n in names}
    metas = [
        LibraryCollectionMeta(
            name=n,
            source_url=f"https://example.test/{n}",
            tags=tags_by_name[n],
            scope_keywords=frozenset(),
        )
        for n in names
    ]
    all_tags = sorted({t for ts in tags_by_name.values() for t in ts})
    monkeypatch.setattr("lies.library.registry.library_collection_metas", lambda: iter(metas))
    monkeypatch.setattr("lies.library.registry.library_collection_names", lambda: frozenset(names))
    monkeypatch.setattr(
        "lies.library.registry.library_collection_tags", lambda: frozenset(all_tags)
    )


def _bypass_daemon_precheck(monkeypatch: pytest.MonkeyPatch, names: list[str]) -> None:
    """Stub the daemon-side pre-check to claim ``names`` is the qmd view.

    The pre-check in ``_search_impl`` calls
    ``access.validate_scope`` to learn the collection list qmd is
    *actually* serving against. Tests that stub ``_post_query`` (and
    so do not exercise the daemon path) need to bypass that read;
    this stub returns ``(names, [])`` so the pre-check passes for
    the same ``names`` the LIES registry returned.
    """
    monkeypatch.setattr(
        "lies.mcp.search._validate_scope_blocking",
        lambda scope: (list(scope), []),
    )


def test_search_passes_question_as_plain_string(monkeypatch: pytest.MonkeyPatch) -> None:
    """Search() forwards the question (or the hypothetical) as a plain string."""
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


def test_search_tag_only_filter_resolves_the_tagged_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``t:``-qualifier-only filter resolves to the collections carrying the tag."""
    from lies.mcp.search import search

    _patch_registry(monkeypatch, ["alpha", "beta"])
    _bypass_daemon_precheck(monkeypatch, ["alpha", "beta"])

    captured: list[Any] = []
    monkeypatch.setattr(
        "lies.mcp.search._post_query",
        lambda doc, scope, limit, timeout: captured.append(scope) or [],
    )
    result = search.fn(question="anything", tag_expr="t:plugins")
    assert result["unknown_tags"] == []
    assert result["searched_scope"] == ["alpha", "beta"]
    assert captured == [["alpha", "beta"]], "scope must reach the daemon, not widen"


def test_search_unqualified_atom_is_an_implicit_t_qualifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``plugins`` and ``t:plugins`` resolve identically."""
    from lies.mcp.search import _resolve_tag_collections

    _patch_registry(monkeypatch, ["alpha", "beta", "gamma"])
    assert _resolve_tag_collections("plugins") == _resolve_tag_collections("t:plugins")
    assert _resolve_tag_collections("plugins") == (["alpha", "beta", "gamma"], [])


def test_search_c_qualifier_still_addresses_a_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``c:`` keeps its collection meaning, and a tag name is not a collection."""
    from lies.mcp.search import _resolve_tag_collections

    _patch_registry(monkeypatch, ["alpha", "beta", "gamma"])
    assert _resolve_tag_collections("c:alpha") == (["alpha"], [])
    resolved, unknown = _resolve_tag_collections("c:plugins")
    assert resolved == []
    assert unknown == ["c:plugins"], "a tag name is not a collection name"


def test_search_tag_expr_set_algebra(monkeypatch: pytest.MonkeyPatch) -> None:
    """``|`` unions and ``&`` intersects, over the collection sets atoms name."""
    from lies.mcp.search import _resolve_tag_collections

    _patch_registry(
        monkeypatch,
        ["alpha", "beta", "gamma"],
        tags_by_name={
            "alpha": frozenset({"plugins"}),
            "beta": frozenset({"plugins"}),
            "gamma": frozenset({"only_gamma"}),
        },
    )
    assert _resolve_tag_collections("plugins|only_gamma") == (["alpha", "beta", "gamma"], [])
    assert _resolve_tag_collections("plugins&only_gamma") == ([], ["plugins&only_gamma"])
    assert _resolve_tag_collections("c:alpha|c:beta") == (["alpha", "beta"], [])
    assert _resolve_tag_collections("c:alpha&t:plugins") == (["alpha"], [])


def test_search_bare_collection_name_is_its_own_implicit_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A collection name resolves through the bare/t: side as itself."""
    from lies.mcp.search import _resolve_tag_collections

    _patch_registry(
        monkeypatch,
        ["alpha", "beta"],
        tags_by_name={"alpha": frozenset(), "beta": frozenset({"plugins"})},
    )
    assert _resolve_tag_collections("alpha") == (["alpha"], [])
    assert _resolve_tag_collections("t:alpha") == (["alpha"], [])
    assert _resolve_tag_collections("c:alpha") == (["alpha"], [])


def test_search_unknown_qualified_tag_marks_unknown_tags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tag no collection carries is unknown, and still refuses the call."""
    from lies.mcp.search import search

    _patch_registry(monkeypatch, ["alpha", "beta"])
    _bypass_daemon_precheck(monkeypatch, ["alpha", "beta"])

    qmd_called: list[Any] = []
    monkeypatch.setattr(
        "lies.mcp.search._post_query",
        lambda *a, **kw: qmd_called.append((a, kw)) or [],
    )
    result = search.fn(question="anything", tag_expr="t:nosuchtag")
    assert result["unknown_tags"] == ["t:nosuchtag"]
    assert result["searched_scope"] == []
    assert result["hits"] == []
    assert qmd_called == [], "must short-circuit rather than widen to the whole library"


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


def test_search_unexpected_post_query_failure_returns_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unexpected error from the seam is folded into the envelope."""
    from lies.mcp.search import search

    _patch_registry(monkeypatch, ["alpha"])
    _bypass_daemon_precheck(monkeypatch, ["alpha"])

    def boom(*a: object, **kw: object) -> list[dict[str, Any]]:
        raise RuntimeError("bridge broke")

    monkeypatch.setattr("lies.mcp.search._post_query", boom)
    result = search.fn(question="anything")
    assert result["transient"] is True
    assert result["no_coverage"] is False
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
    """_post_query delegates to ``access.daemon_tool`` with the library root."""
    from lies.mcp.search import _post_query

    seen: dict[str, Any] = {}

    async def _fake_daemon(name: str, arguments: dict[str, Any], **kw: Any) -> Any:
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
    """An empty ``structuredContent.results`` is mapped to ``[]``."""
    from lies.mcp.search import _post_query

    async def _empty(name: str, arguments: dict[str, Any], **kw: Any) -> Any:
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
    """_post_query strips trailing whitespace for stable wire shape."""
    from lies.mcp.search import _post_query

    captured: dict[str, Any] = {}

    async def _capture(name: str, arguments: dict[str, Any], **kw: Any) -> Any:
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

    async def _fake(name: str, arguments: dict[str, Any], **kw: Any) -> Any:
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

    async def _validate_scope(scope: list[str]) -> tuple[list[str], list[str]]:
        served = set(qmd_collections)
        validated = [n for n in scope if n in served]
        unknown = [n for n in scope if n not in served]
        return validated, unknown

    # Carry the typed exceptions on the SimpleNamespace so the
    # ``except`` clauses can evaluate ``access.QmdDaemonUnavailable``
    # and ``access.QmdDaemonWedged`` even when ``access`` is a fake.
    monkeypatch.setattr(
        "lies.mcp.search.access",
        SimpleNamespace(
            daemon_tool=_fake,
            validate_scope=_validate_scope,
            QmdDaemonUnavailable=QmdDaemonUnavailable,
            QmdDaemonWedged=QmdDaemonWedged,
        ),
    )
    return seen


def test_scoped_search_pushes_the_filter_into_qmd(monkeypatch: pytest.MonkeyPatch) -> None:
    """The resolved collection set reaches the daemon as ``collections``."""
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
    """The envelope's ``limit`` reaches the daemon *and* the LIES-side slice."""
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
    """An unresolvable collection name is reported, not silently widened."""
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
    async def _fake(name: str, arguments: dict[str, Any], **kw: Any) -> Any:
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
    async def _fake_scope(scope: list[str]) -> tuple[list[str], list[str]]:
        served = {"claude_code"}
        return [n for n in scope if n in served], [n for n in scope if n not in served]

    monkeypatch.setattr(
        "lies.mcp.search.access",
        SimpleNamespace(
            daemon_tool=_fake,
            validate_scope=_fake_scope,
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
    """A down daemon is an operator action; it must not be folded into the envelope."""
    from lies.mcp.search import _search_impl
    from lies.qmd.access import QmdDaemonUnavailable, QmdDaemonWedged

    _patch_registry(monkeypatch, ["claude_code"])

    async def _down(name: str, arguments: dict[str, Any], **kw: Any) -> Any:
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

    async def _down_scope(scope: list[str]) -> tuple[list[str], list[str]]:
        raise QmdDaemonUnavailable(
            "qmd daemon is not serving at http://127.0.0.1:8181/mcp. "
            "Start it with 'lies qmd up', or point LIES_QMD_URL at a daemon that is."
        )

    monkeypatch.setattr(
        "lies.mcp.search.access",
        SimpleNamespace(
            daemon_tool=_down,
            validate_scope=_down_scope,
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
    """A wedged daemon is a slow daemon, not an unreachable one."""
    from lies.mcp.search import _search_impl
    from lies.qmd.access import QmdDaemonUnavailable, QmdDaemonWedged

    _patch_registry(monkeypatch, ["claude_code"])

    async def _wedge(name: str, arguments: dict[str, Any], **kw: Any) -> Any:
        if name == "status":
            return SimpleNamespace(
                content=[],
                structured_content={"collections": ["claude_code"]},
                is_error=False,
            )
        raise QmdDaemonWedged(
            "qmd daemon wedged on call to 'query'", last_output="Reranking 40 chunks..."
        )

    async def _wedge_scope(scope: list[str]) -> tuple[list[str], list[str]]:
        raise QmdDaemonWedged(
            "qmd daemon wedged on call to 'status'", last_output="Reranking 40 chunks..."
        )

    monkeypatch.setattr(
        "lies.mcp.search.access",
        SimpleNamespace(
            daemon_tool=_wedge,
            validate_scope=_wedge_scope,
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


def test_a_lock_busy_reads_as_queued_then_timed_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """The RENDERED string, which is the whole user-visible payoff."""
    from lies.lock_errors import QmdLockBusy
    from lies.mcp.search import _search_impl

    _patch_registry(monkeypatch, ["claude_code"])

    async def _busy(name: str, arguments: dict[str, Any], **kw: Any) -> Any:
        if name == "status":
            return SimpleNamespace(
                content=[],
                structured_content={"collections": ["claude_code"]},
                is_error=False,
            )
        raise QmdLockBusy(
            holder_pid=4242,
            waited_s=120.0,
            max_s=120.0,
            lock_path="/var/lib/lies/qmd.lock",
        )

    async def _scope(scope: list[str]) -> tuple[list[str], list[str]]:
        return list(scope), []

    monkeypatch.setattr(
        "lies.mcp.search.access",
        SimpleNamespace(
            daemon_tool=_busy,
            validate_scope=_scope,
            QmdDaemonUnavailable=RuntimeError,
            QmdDaemonWedged=RuntimeError,
        ),
    )

    result = _search_impl("anything")
    reason = result["fallback_reason"] or ""

    # The framing this test exists for.
    assert not reason.startswith("qmd query failed"), reason
    assert "queued behind another qmd operation" in reason, reason
    assert "gave up" in reason, reason
    assert "wait and retry" in reason, reason
    assert "restarting" in reason, reason

    # The exception's facts survive.
    assert "4242" in reason, reason
    assert "120.0" in reason, reason
    assert "/var/lib/lies/qmd.lock" in reason, reason

    # The budget rendered is the raiser's, not the daemon read
    # deadline's — ``get_qmd_query_timeout()`` defaults to 60 and that
    # deadline never fired here.
    assert "120.0s; wait and retry" in reason, (
        f"the rendered budget must be exc.max_s, not the 60s query timeout; got {reason!r}"
    )
    assert "60s" not in reason, reason

    # Still a claim about the run, never about the corpus.
    assert result["transient"] is True
    assert result["no_coverage"] is False
    assert result["hits"] == []


def test_hits_carry_a_path_never_a_docid(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every hit's path is a ``<collection>/<page>`` path, never a docid."""
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
    """A bare question is forwarded as ``lex`` + ``vec``; ``hyde`` is deferred."""
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
    """The pre-check has no cache, so every search pays one daemon ``status`` call."""
    from lies.mcp.search import _search_impl
    from lies.qmd.access import QmdDaemonUnavailable, QmdDaemonWedged

    _patch_registry(monkeypatch, ["claude_code"])

    calls: list[tuple[str, dict[str, Any]]] = []

    async def _fake_daemon_tool(name: str, arguments: dict[str, Any], **kw: Any) -> Any:
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

    async def _fake_validate_scope(scope: list[str]) -> tuple[list[str], list[str]]:
        calls.append(("validate_scope", list(scope)))
        return list(scope), []

    monkeypatch.setattr(
        "lies.mcp.search.access",
        SimpleNamespace(
            daemon_tool=_fake_daemon_tool,
            validate_scope=_fake_validate_scope,
            QmdDaemonUnavailable=QmdDaemonUnavailable,
            QmdDaemonWedged=QmdDaemonWedged,
        ),
    )

    _search_impl("first question")
    _search_impl("second question")

    scope_calls = [c for c in calls if c[0] == "validate_scope"]
    assert len(scope_calls) == 2, (
        f"every search must read the live daemon status (via validate_scope); "
        f"got {len(scope_calls)} validate_scope calls across 2 searches. "
        f"A cache that survived between searches is exactly the staleness "
        f"class this test exists to prevent."
    )
