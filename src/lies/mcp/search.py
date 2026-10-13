"""search MCP tool — single-batch hybrid qmd query against the daemon.

The read path goes through ``lies.qmd.access.daemon_tool`` so the
collection filter is a true push-down rather than a post-filter.

Two envelopes the result must keep separate:

- ``no_coverage`` — *this search found nothing*. A claim about the corpus.
- ``transient`` — *this search did not finish*. A claim about the run.

A *down* daemon (``QmdDaemonUnavailable``) is re-raised so the operator
must act; folding it into ``no_coverage=True`` is the silent-failure
mode this module exists to remove.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastmcp.tools import Tool

from lies.lock_errors import QmdLockBusy
from lies.qmd import access
from lies.query.tag_expr import (
    parse,
)
from lies.qmd.cli import (
    QmdTimeoutError,
)


def _current_timeout() -> int:
    from lies.config import get_qmd_query_timeout

    return get_qmd_query_timeout()


def _decode(stderr: bytes | str) -> str:
    """qmd's captured output as text, bounded.

    ``_run_qmd`` truncates stderr to ``_MAX_STDERR_BYTES`` before it
    reaches here, so this does not need its own cap.
    """
    if isinstance(stderr, bytes):
        return stderr.decode("utf-8", errors="replace").strip()
    return stderr.strip()


def _resolve_tag_collections(tag_expr: str | None) -> tuple[list[str], list[str]]:
    """Resolve ``tag_expr`` body to ``(resolved_collections, unknown_tags)``.

    An atom with no qualifier names a tag; ``t:`` says so explicitly and
    resolves identically; ``c:`` is a strict collection-name match. Per
    the F15 dispatch in :func:`lies.query.synthesizer._collections_matching`,
    a ``t:``/bare atom matches a collection when the atom is one of its
    tags *or* the collection's own name — the implicit self-tag.

    The resolution itself is that shared function rather than a second
    implementation, so a filter ``ground``/``lib_ask`` accepts is a
    filter this tool accepts.

    On a parse error, an atom in neither vocabulary, or a tree that
    selects no collection, the whole ``tag_expr`` is surfaced as one
    unknown-tag entry and the resolved list is empty — the caller learns
    its scope was refused rather than being handed a search over the
    whole library it did not ask for. Returns ``sorted`` for determinism.
    """
    if tag_expr is None:
        return [], []
    # Imported here: ``mcp.server`` imports this module.
    from lies.mcp.server import _collect_available_tags_mcp
    from lies.query.synthesizer import _collections_matching
    from lies.query.tag_expr import (
        TagExprUnknown,
        resolve,
    )

    try:
        resolved = resolve(
            parse(tag_expr),
            available=_collect_available_tags_mcp(wiki=None),
        )
    except TagExprUnknown:
        return [], [tag_expr]
    except Exception:
        return [], [tag_expr]

    scoped = _collections_matching(resolved)
    if not scoped:
        return [], [tag_expr]
    return sorted(scoped), []


def _validate_scope_blocking(scope: list[str]) -> tuple[list[str], list[str]]:
    """Run :func:`access.validate_scope` from a sync call site, loop-safe."""
    coro = access.validate_scope(scope)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="search-bridge") as pool:
        future = pool.submit(asyncio.run, coro)
        result = future.result()
        assert isinstance(result, tuple) and len(result) == 2
        return result


def _run_blocking(coro: Any) -> Any:
    """Run an async coroutine from a sync call site, loop-safe.

    FastMCP runs sync handlers in a threadpool; ``asyncio.run`` works
    there. A running loop is reachable from an agent run — and
    ``asyncio.run`` from inside one raises ``RuntimeError``. The
    running-loop case runs the coroutine on its own thread with its
    own loop instead.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="search-bridge") as pool:
        return pool.submit(asyncio.run, coro).result()


def _normalize_daemon_hit(row: dict[str, Any]) -> dict[str, Any]:
    """Map a daemon ``query`` row onto the existing internal ``path`` contract.

    The daemon returns ``file`` (``displayPath``), which is already the
    ``<collection>/<page>`` form downstream consumers read. Docids are
    not the path: ``docid`` is ``documents.hash[0:6]`` and has live
    collisions among 5987 active documents, so a path keyed by docid
    silently returns the wrong file on collision.
    """
    file_value = row.get("file")
    if not isinstance(file_value, str) or not file_value:
        return {}
    out = dict(row)
    out["path"] = file_value
    return out


def _post_query(doc: str, scope: list[str], limit: int, timeout: int) -> list[dict[str, Any]]:
    """Issue one daemon ``query`` against the library index, scoped to ``scope``.

    The daemon's ``collections`` parameter is a true push-down. ``limit``
    is forwarded and the result is sliced to the same length — defence
    in depth against a backend that ignores the wire argument (the
    CLI's ``--limit`` was inert).

    ``timeout`` is the per-call read deadline forwarded to
    ``fastmcp.Client.call_tool`` via the seam.
    """
    doc = doc.rstrip()

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

    structured = getattr(result, "structured_content", None) or {}
    rows = structured.get("results") or []
    if not isinstance(rows, list):
        rows = []

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

    Returns a dict matching the ``SearchResult`` shape. ``no_coverage``
    and ``transient`` answer different questions and must not be
    collapsed: ``no_coverage`` is a claim about the corpus, ``transient``
    is a claim about the run. A *down* daemon (``QmdDaemonUnavailable``)
    is re-raised so the operator must act.

    ``limit`` is forwarded to the daemon *and* enforced on the response
    (see :func:`_post_query`).
    """
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
        # ``tag_expr is None`` widens to the whole library. One that
        # resolved to nothing is a real scope miss: widening silently
        # would answer from the whole library with no word about the
        # dropped tag.
        if tag_expr is not None:
            return {
                "hit": None,
                "hits": [],
                "unknown_tags": [tag_expr],
                "no_coverage": False,
                "transient": False,
                "searched_scope": [],
                "fallback_reason": f"tag expression resolved to no collections: {tag_expr!r}",
            }
        from lies.library.registry import library_collection_names

        scope = sorted(library_collection_names())

    if not scope:
        return {
            "hit": None,
            "hits": [],
            "unknown_tags": [],
            "no_coverage": False,
            "transient": False,
            "searched_scope": [],
            "fallback_reason": "no collections registered",
        }

    # The daemon answers an unknown collection with an empty result
    # and **no error**, so its ``status`` is the binding truth —
    # a name in the LIES registry can be absent from qmd.
    try:
        scope, unknown = _validate_scope_blocking(scope)
    except access.QmdDaemonUnavailable:
        raise
    except access.QmdDaemonWedged as exc:
        detail = f"; last qmd output: {exc.last_output!r}" if exc.last_output else ""
        return {
            "hit": None,
            "hits": [],
            "unknown_tags": [],
            "no_coverage": False,
            "transient": True,
            "searched_scope": [],
            "fallback_reason": f"qmd wedged during scope validation{detail}",
        }
    if unknown:
        return {
            "hit": None,
            "hits": [],
            "unknown_tags": [tag_expr] if tag_expr else unknown,
            "no_coverage": False,
            "transient": False,
            "searched_scope": [],
            "fallback_reason": f"unknown tag: {tag_expr!r}" if tag_expr else "unknown tag",
        }

    doc = hypothetical or question

    try:
        raw = _post_query(doc, scope, limit=limit, timeout=_current_timeout())
    except access.QmdDaemonUnavailable:
        raise
    except access.QmdDaemonWedged as exc:
        # A wedge is a slow daemon that the seam recycled, not a
        # statement about the corpus. ``transient`` is the honest
        # "ask again" flag.
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
        # CLI-side fallback, unreachable on this path: the seam raises
        # ``QmdDaemonWedged`` for a daemon-side timeout and that is
        # caught above. The answer is ``transient=True`` regardless.
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
    except QmdLockBusy as exc:
        # Ahead of the untyped arm, and the site where it is reachable:
        # the daemon answered fine and this call queued, so the action
        # is wait, not restart. The budget is ``exc.max_s``, not
        # ``_current_timeout()`` — different clocks.
        return {
            "hit": None,
            "hits": [],
            "unknown_tags": [],
            "no_coverage": False,
            "transient": True,
            "searched_scope": scope,
            "fallback_reason": (
                f"queued behind another qmd operation and gave up after "
                f"{exc.max_s}s; wait and retry rather than restarting "
                f"the daemon ({exc})"
            ),
        }
    except Exception as exc:  # noqa: BLE001
        # Anything outside the typed taxonomy (``QmdDaemonUnavailable``
        # is re-raised; ``QmdDaemonWedged`` is caught above) is a
        # process failure, not a statement about the corpus. Mapping
        # to ``no_coverage=True`` would be a false corpus claim.
        return {
            "hit": None,
            "hits": [],
            "unknown_tags": [],
            "no_coverage": False,
            "transient": True,
            "searched_scope": scope,
            "fallback_reason": f"qmd query failed: {type(exc).__name__}: {exc}",
        }

    hits = list(raw)
    no_coverage = not hits
    hit = hits[0] if hits else None

    # A registered-but-empty collection is in the daemon's list with
    # zero documents, so it returns zero hits — which ``no_coverage``
    # alone would read as "the corpus has nothing". Keep the flag and
    # name the empty collection.
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


# Wrap as a FastMCP ``Tool`` so the wire can serialize; ``search.fn``
# reaches the underlying function for tests.
search = Tool.from_function(_search_impl, name="search")
