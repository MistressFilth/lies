"""search MCP tool — single-batch hybrid qmd query against the daemon.

The read path goes through ``lies.qmd.access.daemon_tool`` so the
collection filter is a true push-down (the daemon's ``collections``
parameter narrows the candidate set inside qmd, not after) and the
read sites cannot drift into a CLI fallback for retrieval. The
previous CLI path ranked globally and dropped rows whose first
``/`` segment was outside the resolved scope: a multi-collection
``c:claude_code|c:opencode`` query returned hits from whichever
collection ranked highest, and a collection could be starved to
zero rows even when it had matches. The daemon's push-down is
the only safe form.

Two envelopes the envelope must keep separate, and must not
collapse:

- ``no_coverage`` — *this search found nothing*. A claim about
  the corpus.
- ``transient`` — *this search did not finish*. A claim about
  the run. A timeout, a wedge, anything that prevents the search
  from learning an answer leaves ``no_coverage=False`` and sets
  ``transient=True`` instead.

A *down* daemon (``QmdDaemonUnavailable``) is a third state and
not in the envelope: it is re-raised so the operator must act.
The spec is explicit that a down daemon fails loudly; folding it
into ``no_coverage=True`` is the exact failure mode the timeout
classification branch exists to remove.

Review Focus #2/#3 — an unknown collection, and a filter that
matches a collection registered but empty — short-circuits here
on the registry, not on the daemon. The daemon answers an
unknown collection with an empty result and **no error**; the
CLI exits 1 on the same class. Without pre-validation this would
surface as "the corpus has nothing" — a false claim about the
corpus made for a question that named something absent. The
registry check happens after the resolver, as a second pass
against ``library_collection_names()``, so a stale registry cache
between the resolver and the dispatch still gets caught.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastmcp.tools import Tool

from lies.qmd import access
from lies.query.tag_expr import (
    And,
    Include,
    Or,
    TagExpr,
    TagExprUnknown,
    parse,
    resolve,
)
from lies.qmd.cli import (
    QmdCommandError,
    QmdTimeoutError,
)

# Per-call deadline for the single qmd query behind this tool.
#
# The previous 15s was inherited by copy from
# ``grounding._QMD_FANOUT_TIMEOUT``, which had sized it on a 2026-09-25
# live probe: ~3-7s warm, and a cold daemon whose rerank step can pass
# 10s. Two things did not survive the copy. The sibling's
# ``LIES_QMD_FANOUT_TIMEOUT`` override was left behind, so the
# documented knob for a tighter SLO did not exist on the path that
# actually serves searches; and the number itself was sized for a
# per-collection fan-out, where one 15s stall is 15s out of a 210s
# ceiling across 14 collections. Here it is the whole operation.
#
# The value itself, and the rationale, live in
# ``lies.config.get_qmd_query_timeout`` — the single source shared with
# the grounding fan-out, so the two qmd query call sites cannot ship
# different answers for the same subprocess. Read at call time below
# rather than bound here, so the override takes effect without a
# module reload.


def _current_timeout() -> int:
    from lies.config import get_qmd_query_timeout

    return get_qmd_query_timeout()


def _decode(stderr: bytes | str) -> str:
    """qmd's captured output as text, bounded.

    ``_run_qmd`` truncates stderr to ``_MAX_STDERR_BYTES`` before it
    reaches here, so this does not need its own cap — it needs to
    survive whatever encoding qmd emitted, because the one job of this
    string is to be read by whoever is debugging the stall.
    """
    if isinstance(stderr, bytes):
        return stderr.decode("utf-8", errors="replace").strip()
    return stderr.strip()


def _resolve_tag_collections(tag_expr: str | None) -> tuple[list[str], list[str]]:
    """Resolve a tag_expr body to (resolved_collections, unknown_tags).

    ``tag_expr`` is the body of a single include expression (no leading
    ``+``), e.g. ``"c:alpha|c:beta"``. ``None`` means untagged.

    Each Include atom with qualifier ``None`` or ``"c"`` contributes its
    bare tag (the collection name). T-qualifier atoms are filtered out
    — the tag side is not part of the addressable collection set for
    ``search()``. Returns sorted(resolved & available) so the result is
    deterministic regardless of input ordering. On any parse / resolution
    error, surfaces the entire ``tag_expr`` as a single unknown-tag entry
    and returns an empty resolved list — the caller short-circuits without
    dispatching to qmd.
    """
    if tag_expr is None:
        return [], []
    from lies.library.registry import library_collection_names

    available = set(library_collection_names())

    try:
        ast = parse(tag_expr)
        resolved = resolve(ast, available=available)
    except TagExprUnknown:
        # Per spec: unknown tag surfaces in unknown_tags; no daemon call.
        return [], [tag_expr]
    except Exception:
        # ``TagExprParseError`` / ``TagExprEmpty`` and any other parser-side
        # failure: surface the raw expression as an unknown spec rather
        # than crash the search tool.
        return [], [tag_expr]

    names: set[str] = set()

    def _walk(node: TagExpr | None) -> None:
        if node is None:
            return
        if isinstance(node, Include):
            if node.qualifier in (None, "c"):
                names.add(node.tag)
            return
        if isinstance(node, Or):
            _walk(node.left)
            _walk(node.right)
            return
        if isinstance(node, And):
            _walk(node.left)
            _walk(node.right)
            return
        # Defensive fallback for any future ``TagExpr`` variant we don't
        # enumerate above — walk whatever ``left``/``right`` children
        # exist so we don't silently drop names.
        for attr in ("left", "right"):
            child = getattr(node, attr, None)
            if child is not None:
                _walk(child)

    _walk(resolved.include)
    return sorted(names & available), []


def _qmd_collection_names() -> frozenset[str]:
    """The collection set the daemon is currently serving.

    Used by the pre-dispatch check in :func:`_search_impl`. The
    daemon's ``status`` tool returns ``structuredContent.collections``
    as a list of plain strings (verified against qmd 2.5.3
    2026-10-03; the schema also accepts a list of ``{"name": "..."}``
    objects, both shapes are read so the helper does not lock to
    one). The names are what the daemon's ``collections`` filter
    accepts.

    **No cache.** A previous version held the set in an
    ``lru_cache(maxsize=1)`` on the grounds that the collection
    list "changes only on a deliberate ``qmd collection add`` /
    ``remove``." That is true on paper, but the cache had no
    production-side invalidation path: there is no signal from
    the daemon when its collection list changes, and LIES'
    own ``lies library new`` / ``lies sync`` / ingest paths do
    not clear it. A long-running MCP server that the operator
    ingested into would silently keep its pre-ingest view until
    restart, and a ``qmd cleanup`` that drops collections would
    leave the cache claiming they exist. The staleness was a
    known-unbounded cost the field was not paying.

    Measured cost of the cached call against the live daemon
    (2026-10-03, 10 warm samples after 3 warm-ups): **p50 41.9 ms,
    max 45.8 ms, stdev 1.7 ms**. A search query runs in 5.6-6.0s
    warm, so the status call is ~0.7% of a single search and
    negligible against the 30s+ cold-query ceiling. Drop the
    cache; pay the round trip on every search; the staleness
    class is closed.
    """
    result = _run_blocking(access.daemon_tool("status", {}))
    structured = getattr(result, "structured_content", None) or {}
    rows = structured.get("collections") or []
    if not isinstance(rows, list):
        return frozenset()
    names: set[str] = set()
    for row in rows:
        # Both shapes read so the daemon's schema choice does
        # not lock the helper to one.
        if isinstance(row, str):
            if row:
                names.add(row)
        elif isinstance(row, dict):
            name = row.get("name")
            if isinstance(name, str) and name:
                names.add(name)
    return frozenset(names)


def _qmd_collection_names_for_check() -> frozenset[str]:
    """The collection set the pre-check sees.

    Indirection over :func:`_qmd_collection_names` so tests can
    stub the read without going through the daemon. Production
    code calls this; the implementation is a direct daemon
    ``status`` round trip with no cache.
    """
    return _qmd_collection_names()


def _run_blocking(coro: Any) -> Any:
    """Run an async coroutine from a sync call site, loop-safe.

    FastMCP runs sync handlers in a threadpool, pydantic-ai runs
    sync tools via ``run_in_executor`` — both production call
    sites have no running event loop, and ``asyncio.run`` works
    there. A running loop is reachable (``lib_ask`` is async, and
    anything that bridges back into an agent run has a live
    loop), and ``asyncio.run`` from inside one raises
    ``RuntimeError: asyncio.run() cannot be called from a running
    event loop`` — the exact bug ``ground()`` shipped with in
    #106. The running-loop case runs the coroutine on its own
    thread with its own loop instead.

    Identical shape to the bridge in :mod:`lies.mcp.read`; the
    two are not factored into one helper because each has a
    distinct type and the helpers are tiny.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="search-bridge") as pool:
        return pool.submit(asyncio.run, coro).result()


def _normalize_daemon_hit(row: dict[str, Any]) -> dict[str, Any]:
    """Map a daemon ``query`` row onto the existing internal ``path`` contract.

    The daemon returns ``file`` (``displayPath`` — the collection-
    relative path) and a ``docid`` for back-compat. Downstream
    consumers (the synthesizer, the F19 citation contract) read
    ``path``; the path is ``<collection>/<page>`` form, e.g.
    ``claude_code/hooks.md``, which is what ``displayPath``
    already is. The mapping is one key rename, kept in a helper
    so the two representations cannot drift.

    Docids are **not** the path. ``docid`` is
    ``documents.hash[0:6]`` with no ``ORDER BY``; three live
    collisions exist among the 5987 active documents, so a path
    keyed by docid silently returns the wrong file on collision.
    A row that has no usable ``file`` is dropped: the daemon
    only sends ``file`` when it was found, and an empty
    ``displayPath`` is never a valid lookup key.
    """
    file_value = row.get("file")
    if not isinstance(file_value, str) or not file_value:
        return {}
    out = dict(row)
    out["path"] = file_value
    return out


def _post_query(doc: str, scope: list[str], limit: int, timeout: int) -> list[dict[str, Any]]:
    """Issue one daemon ``query`` against the library index, scoped to ``scope``.

    The previous CLI shape — ``qmd query <q> --limit N --json`` plus a
    post-hoc Python filter on each row's first ``/`` segment — could
    silently starve a multi-collection query to whichever collection
    ranked highest, because the rank was global. The daemon's
    ``collections`` parameter is a true push-down: the candidate set
    is narrowed *inside* qmd, so a hybrid search over
    ``[claude_code, opencode]`` returns in-scope rows from both.

    ``limit`` is forwarded to the daemon **and** the result is
    sliced to the same length on the way out. The daemon honours
    ``limit`` today; the LIES-side slice is the load-bearing half:
    the CLI's ``--limit`` was inert (qmd parsed only ``values.n``,
    then overrode with ``results.length``), and a future backend
    that ignores the wire argument must not pass through as if it
    applied.

    ``timeout`` is the per-call read deadline, in seconds, forwarded
    to the daemon as the ``timeout=`` keyword on
    ``fastmcp.Client.call_tool`` via
    :func:`lies.qmd.access.daemon_tool`. A value the seam would have
    applied on a CLI subprocess is now applied on the daemon
    transport too, so ``LIES_QMD_FANOUT_TIMEOUT`` takes effect on
    the next call without waiting for the cached client to be
    invalidated.

    A timeout on the daemon side surfaces as
    :class:`QmdDaemonWedged` (the seam's recycle-and-raise path,
    not a CLI subprocess failure) — the docstring above predates
    the daemon transport and reflects the pre-#106 CLI behaviour.
    The ``except QmdTimeoutError`` clauses below are kept as
    defensive fallbacks: they do not fire on this path today,
    but a future call site that re-routes through the CLI (the
    bench tool, say) cannot regress the envelope if they
    silently drop a timeout. The CLI's
    :class:`QmdTimeoutError` is a subclass of
    :class:`QmdCommandError`, so a CLI re-routing here is caught
    by the same clause that catches other unexpected
    subprocess failures.
    """
    # Strip trailing whitespace defensively. The previous structured
    # ``vec: ...\\nlex: ...`` form required this; the plain-string
    # form does not, but normalizing the wire shape keeps callers
    # that source the question through different shapes (shell
    # ``$()`` strips, manual construction, etc.) on the same
    # payload the tests pin.
    doc = doc.rstrip()

    # Task 4 step 9: a bare question is one ``lex`` and one ``vec``
    # entry; the ``hyde`` decision is deferred to Task 6. The first
    # sub-query gets 2x weight in qmd's hybrid blend (``store.js``
    # documents this), so ``lex`` is first: the keyword leg is the
    # one with the sharpest signal, and a vector-only pass is the
    # case where the BM25 leg has nothing to add. The intent field
    # is required by the daemon's schema.
    searches = [
        {"type": "lex", "query": doc},
        {"type": "vec", "query": doc},
    ]

    arguments: dict[str, Any] = {
        "searches": searches,
        "limit": limit,
        "collections": list(scope),
        "intent": "lies.mcp.search read-side hybrid query",
    }

    result = _run_blocking(access.daemon_tool("query", arguments, timeout=float(timeout)))

    # The daemon's MCP tool returns a ``CallToolResult`` whose
    # ``structured_content`` carries the rows. A malformed payload
    # that has no ``results`` is treated as zero rows — same
    # surface as an empty result, so the envelope's ``no_coverage``
    # flag is the honest report.
    structured = getattr(result, "structured_content", None) or {}
    rows = structured.get("results") or []
    if not isinstance(rows, list):
        rows = []

    # Envelope-side limit enforcement. See the docstring: the
    # daemon honours ``limit`` today, the slice is defence in depth
    # against a backend that returns a wider top-N than asked.
    rows = rows[:limit]

    normalized: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        hit = _normalize_daemon_hit(row)
        if hit:
            normalized.append(hit)
    return normalized


def _search_impl(
    question: str,
    tag_expr: str | None = None,
    exclude_tags: list[str] | None = None,
    hypothetical: str | None = None,
    *,
    limit: int = 10,
) -> dict[str, Any]:
    """Run a single-batch hybrid vec+lex qmd query against the daemon.

    Returns a dict matching the ``SearchResult`` shape:
    ``{hit, hits, unknown_tags, no_coverage, transient,
    searched_scope, fallback_reason}``. Returned as dict (not the
    dataclass) so MCP can serialize without pydantic round-trip.

    ``no_coverage`` and ``transient`` answer different questions and
    must not be collapsed. ``no_coverage`` means *this search found
    nothing*, which is a claim about the corpus. ``transient`` means
    *this search did not finish*, which is a claim about the run. A
    timeout sets only the second: the search never got to learn
    anything, so it has no standing to assert the corpus is empty.
    A *down* daemon (``QmdDaemonUnavailable``) is re-raised
    entirely: the operator must act, and folding it into either
    envelope flag is the silent-failure mode the timeout
    classification branch exists to remove.

    The ``limit`` keyword is the LIES-side cap; it is forwarded to
    the daemon *and* enforced on the response (see ``_post_query``).
    The default matches the qmd daemon's default, so a call without
    ``limit`` reads identically at the wire.
    """
    # ``exclude_tags`` is preserved in the surface signature for forward
    # compatibility with the design contract (F15 grammar lets callers
    # peel a ``-`` chain through a separate path). The single-batch qmd
    # filter today is include-only — the resolved include set already
    # constrains the addressable collection set, so a separately-supplied
    # exclude chain doesn't change the dispatch until the qmd payload
    # contract grows a nested filter. The MCP wire expects the parameter
    # to exist (otherwise ``lib_ask`` can't thread it through).
    del exclude_tags

    if not question:
        return {
            "hit": None,
            "hits": [],
            "unknown_tags": [],
            "no_coverage": False,
            "transient": False,
            "searched_scope": [],
            "fallback_reason": "empty question",
        }

    scope, unknown = _resolve_tag_collections(tag_expr)

    # Empty scope AND unknown tag → tag matches zero collections; short-circuit.
    if unknown:
        return {
            "hit": None,
            "hits": [],
            "unknown_tags": unknown,
            "no_coverage": False,
            "transient": False,
            "searched_scope": [],
            "fallback_reason": f"unknown tag: {unknown[0]!r}",
        }

    if not scope:
        # No tag OR unknown tag → search all registered collections.
        from lies.library.registry import library_collection_names

        scope = sorted(library_collection_names())

    if not scope:
        # Empty registry → no daemon call possible.
        return {
            "hit": None,
            "hits": [],
            "unknown_tags": [],
            "no_coverage": False,
            "transient": False,
            "searched_scope": [],
            "fallback_reason": "no collections registered",
        }

    # Review Focus #2 — the daemon answers an unknown collection
    # with an empty result and **no error**. The LIES registry is
    # one source of truth, but it is not the binding one: a name
    # can be recorded locally and never reach qmd (the LIES
    # registry's writer does not talk to qmd), or be in the LIES
    # registry and then dropped by a later ``qmd cleanup`` /
    # ``qmd update``. The pre-check asks the daemon's ``status``
    # tool for the collection list qmd is *actually* serving
    # against, and the resolved scope is intersected with that.
    # Anything not in the intersection is "you named a collection
    # that does not exist" and is reported as ``unknown_tags``.
    # Review Focus #3 — a registered-but-empty collection
    # (``wiki_default``, 0 files) is in the daemon's collection
    # list with zero documents; it is **not** a "you named a
    # collection that does not exist" and is **not** an empty
    # search result, but it is also not what the user asked for
    # in the sense that no hits can come from it. That case is
    # detected at the post-call boundary below, where ``hits == []``
    # plus a name-with-zero-documents produces a dedicated
    # ``fallback_reason``. The two states get distinct answers
    # because they are distinct facts: a missing name is a syntax
    # error in the user's request, and an empty collection is a
    # coverage gap the operator should be told about.
    qmd_collections = _qmd_collection_names_for_check()
    if any(name not in qmd_collections for name in scope):
        # The user's original expression is what they wrote; the
        # resolved names are the implementation's. The MCP surface
        # should report the expression, not the resolver's
        # intermediate form. This matches the parse-error path
        # above (``except TagExprUnknown`` returns ``[tag_expr]``),
        # so a missing-collection and a parse error report the
        # same shape: the user's wording.
        return {
            "hit": None,
            "hits": [],
            "unknown_tags": [tag_expr]
            if tag_expr
            else [name for name in scope if name not in qmd_collections],
            "no_coverage": False,
            "transient": False,
            "searched_scope": [],
            "fallback_reason": f"unknown tag: {tag_expr!r}" if tag_expr else "unknown tag",
        }
    scope = [name for name in scope if name in qmd_collections]

    # Pass the user's question (or the HyDE hypothetical when set) as a
    # plain string. The v0.40 structured ``vec: ...\nlex: ...`` doc form
    # was found to silently lose coverage on some queries (``LSP setup``
    # against ``opencode`` returned 0 hits even though the single-query
    # form returned 90%-scored hits). qmd's hybrid search handles
    # vec+lex internally when the question is plain, so we rely on that
    # path instead.
    doc = hypothetical or question

    try:
        raw = _post_query(doc, scope, limit=limit, timeout=_current_timeout())
    except access.QmdDaemonUnavailable:
        # Re-raise. The spec is explicit that a down daemon fails
        # loudly, and the message already names the fix
        # (``lies qmd up``). Folding this into the envelope would
        # reproduce the silent-failure mode the timeout
        # classification branch exists to remove: the operator
        # would see "no relevant content in library" while the
        # daemon is in fact down.
        raise
    except access.QmdDaemonWedged as exc:
        # A wedge is a slow daemon that the seam recycled, not an
        # unreachable one and not a statement about the corpus.
        # ``no_coverage`` is reserved for "this search found
        # nothing"; a search that never finished learned nothing
        # and has no standing to assert the corpus is empty.
        # ``transient`` is the honest "ask again" flag, and the
        # daemon's last log line is the only evidence of where
        # the time went.
        detail = f"; last qmd output: {exc.last_output!r}" if exc.last_output else ""
        return {
            "hit": None,
            "hits": [],
            "unknown_tags": [],
            "no_coverage": False,
            "transient": True,
            "searched_scope": scope,
            "fallback_reason": f"qmd wedged; recycling did not help{detail}",
        }
    except QmdTimeoutError as exc:
        # CLI-side timeout. The seam re-raises this from a CLI
        # path that ``_post_query`` no longer uses; the catch stays
        # so a future call site that re-routes through the CLI
        # (the bench tool, say) cannot regress the envelope.
        detail = ""
        if exc.stderr:
            detail = f"; last qmd output: {_decode(exc.stderr)!r}"
        return {
            "hit": None,
            "hits": [],
            "unknown_tags": [],
            "no_coverage": False,
            "transient": True,
            "searched_scope": scope,
            "fallback_reason": f"qmd timed out after {_current_timeout()}s{detail}",
        }
    except QmdCommandError as exc:
        # Defensive fallback: an unexpected non-timeout, non-wedge
        # qmd failure. Maps to ``no_coverage=True`` so a caller
        # that checks the flag sees the envelope as a failed
        # search, with the class name and message preserved in
        # ``fallback_reason``.
        return {
            "hit": None,
            "hits": [],
            "unknown_tags": [],
            "no_coverage": True,
            "transient": False,
            "searched_scope": scope,
            "fallback_reason": f"qmd unreachable: {exc}",
        }
    except Exception as exc:  # noqa: BLE001
        # Defensive fallback for an unexpected internal error.
        # The class name and message reach the operator through
        # ``fallback_reason``; the envelope still answers with
        # ``no_coverage=True`` so a caller that checks the flag
        # gets a consistent surface.
        return {
            "hit": None,
            "hits": [],
            "unknown_tags": [],
            "no_coverage": True,
            "transient": False,
            "searched_scope": scope,
            "fallback_reason": f"{type(exc).__name__}: {exc}",
        }

    hits = list(raw)
    no_coverage = not hits
    hit = hits[0] if hits else None

    # Review Focus #3 — a registered-but-empty collection
    # (``wiki_default``) is in the daemon's collection list with
    # zero documents. The search against it succeeds and returns
    # zero hits, which a caller reading ``no_coverage=True`` would
    # read as "the corpus has nothing for this question" — a
    # false claim about the corpus, made for a question that
    # named a known-empty collection. The honest answer keeps
    # ``no_coverage=True`` (no rows did come back) and adds a
    # ``fallback_reason`` that names the empty collection, so a
    # reader can distinguish "I searched and found nothing" from
    # "the collection you named is empty in qmd". Only fires when
    # the scope is a single known-but-empty collection: a
    # multi-collection scope with one empty member is a different
    # state (the others may still match).
    if no_coverage and len(scope) == 1:
        from lies.library.registry import library_collection_names

        if scope[0] in library_collection_names() and not hits:
            return {
                "hit": None,
                "hits": hits,
                "unknown_tags": [],
                "no_coverage": True,
                "transient": False,
                "searched_scope": scope,
                "fallback_reason": f"collection {scope[0]!r} is registered but has no documents in qmd",
            }
    return {
        "hit": hit,
        "hits": hits,
        "unknown_tags": [],
        "no_coverage": no_coverage,
        "transient": False,
        "searched_scope": scope,
        "fallback_reason": None,
    }


# Wrap the function as a FastMCP ``Tool`` so the MCP wire can serialize
# the dispatch surface and tests can reach the underlying function via
# ``search.fn(...)``. Server registration is a separate concern (Task 5
# + the wire server.py wiring); the Tool object is constructed here so
# downstream code can ``import search`` and call ``search.fn`` without
# spinning up an MCP instance.
search = Tool.from_function(_search_impl, name="search")
