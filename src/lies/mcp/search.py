"""search MCP tool — single-batch hybrid qmd query.

Replaces the old per-collection fan-out in ``_fanout_collections`` with
one qmd call carrying the resolved collection set as ``collection_filter``.
qmd's hybrid search handles vec+lex internally; we pass the user's
question as a plain string (or the HyDE hypothetical when set).
Library-wins-on-slug-conflict merge happens inside qmd.

Specs: docs/superpowers/specs/2026-09-26-librarian-v040-port-design.md.
"""

from __future__ import annotations

import os
from typing import Any

from fastmcp.tools import Tool

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
    QmdNoResultsError,
    QmdTimeoutError,
    qmd_query,
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
# Live measurements against the 5987-doc corpus (2026-10-01): warm
# 5.6-6.0s, 3 concurrent clients 5.9-6.6s, no timeouts in ~150 calls.
# Intermittent stalls past 15s do occur under host contention. 60s is
# ``qmd_query``'s own default, so this is no longer stricter than the
# layer beneath it, and it leaves room for a cold start on top of the
# warm cost. Override with ``LIES_QMD_FANOUT_TIMEOUT`` — the same
# variable the fan-out reads, so one knob governs both.
_SEARCH_TIMEOUT = int(os.environ.get("LIES_QMD_FANOUT_TIMEOUT", "60"))


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


def _post_query(doc: str, scope: list[str], limit: int, timeout: int) -> list[dict[str, Any]]:
    """Issue one qmd query against the library index.

    Production wiring. Delegates to :func:`lies.qmd.cli.qmd_query` with
    the question as a plain string payload and the resolved collection
    set as the ``collection_filter``. qmd's hybrid search handles
    vec+lex internally. Returns an empty list when
    :class:`QmdNoResultsError` fires (the caller maps that to
    ``no_coverage=True``). ``QmdTimeoutError`` and the other
    ``QmdCommandError`` propagate so the caller can label a slow daemon
    differently from a broken one.
    """
    from lies.library.registry import library_git_root

    cwd = library_git_root()
    collection_filter = set(scope) if scope else None

    # Strip trailing whitespace defensively. The original structured
    # doc form required this (qmd rejected empty trailing lines from
    # f-string interpolation); the plain-string form doesn't, but
    # normalizing the wire shape keeps callers that source the
    # question through different shapes (shell ``$()`` strips, manual
    # construction, etc.) on the same payload the tests pin.
    doc = doc.rstrip()

    try:
        return qmd_query(
            cwd=cwd,
            question=doc,
            limit=limit,
            timeout=timeout,
            collection_filter=collection_filter,
        )
    except QmdNoResultsError:
        return []


def _search_impl(
    question: str,
    tag_expr: str | None = None,
    exclude_tags: list[str] | None = None,
    hypothetical: str | None = None,
) -> dict[str, Any]:
    """Run a single-batch hybrid vec+lex qmd query.

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

    # Pass the user's question (or the HyDE hypothetical when set) as a
    # plain string. The v0.40 structured ``vec: ...\nlex: ...`` doc form
    # was found to silently lose coverage on some queries (``LSP setup``
    # against ``opencode`` returned 0 hits even though the single-query
    # form returned 90%-scored hits). qmd's hybrid search handles
    # vec+lex internally when the question is plain, so we rely on that
    # path instead.
    doc = hypothetical or question

    try:
        raw = _post_query(doc, scope, limit=10, timeout=_SEARCH_TIMEOUT)
    except QmdTimeoutError as exc:
        # A timeout is a slow daemon, not an absent one, and it is not
        # a statement about the corpus. Reporting it as
        # ``qmd unreachable`` + ``no_coverage=True`` sent a stalled
        # call to the user as "No relevant content found in library."
        # — a false claim about what the library contains, for a query
        # that returns in under six seconds on a retry.
        #
        # ``no_coverage`` is the flag that means "this search found
        # nothing", and a search that never finished learned nothing.
        # ``transient`` is the new one: the honest description is
        # "ask again", and the librarian contract is what turns a flag
        # into prose a model acts on.
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
            "fallback_reason": f"qmd timed out after {_SEARCH_TIMEOUT}s{detail}",
        }
    except QmdCommandError as exc:
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
