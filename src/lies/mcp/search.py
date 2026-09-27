"""search MCP tool — single-batch hybrid vec+lex qmd query.

Replaces the old per-collection fan-out in ``_fanout_collections`` with
one qmd call carrying the resolved collection set as ``collection_filter``
and a structured ``vec + lex`` query doc. Library-wins-on-slug-conflict
merge happens inside qmd.

Specs: docs/superpowers/specs/2026-09-26-librarian-v040-port-design.md.
"""

from __future__ import annotations

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


def _build_query_doc(question: str, scope: list[str] | None = None) -> str:
    """Build the structured vec+lex query doc qmd accepts.

    The qmd daemon's structured-query input accepts lines of the form
    ``vec: <query>`` and ``lex: <query>``; hybrid search submits both
    and globally re-ranks the union. ``scope`` is reserved for future
    per-collection metadata that may be embedded into the doc (today
    the resolved collection set rides along via ``collection_filter``
    on the qmd call itself, not in the doc body). Accepted as a
    keyword argument so the helper's signature is forward-compatible
    without a per-caller churn.
    """
    del scope  # see docstring; not embedded into the doc today
    return f"vec: {question}\nlex: {question}\n"


def _post_query(doc: str, scope: list[str], limit: int, timeout: int) -> list[dict[str, Any]]:
    """Issue one qmd query. Patched in tests.

    Production impl uses :func:`lies.qmd.cli.qmd_query` with the
    structured doc written to a temp file and passed via the qmd CLI's
    document-format flag.
    """
    raise NotImplementedError("production wiring in Task 7")


def _search_impl(
    question: str,
    tag_expr: str | None = None,
    exclude_tags: list[str] | None = None,
    hypothetical: str | None = None,
) -> dict[str, Any]:
    """Run a single-batch hybrid vec+lex qmd query.

    Returns a dict matching the ``SearchResult`` shape:
    ``{hit, hits, unknown_tags, no_coverage, searched_scope,
    fallback_reason}``. Returned as dict (not the dataclass) so MCP
    can serialize without pydantic round-trip.
    """
    # ``exclude_tags`` is preserved in the surface signature for forward
    # compatibility with the design contract (F15 grammar lets callers
    # peel a ``-`` chain through a separate path). The single-batch qmd
    # filter today is include-only — the resolved include set already
    # constrains the addressable collection set, so a separately-supplied
    # exclude chain doesn't change the dispatch until the qmd payload
    # contract grows a nested filter. The MCP wire expects the parameter
    # to exist (otherwise `ask` can't thread it through).
    del exclude_tags

    if not question:
        return {
            "hit": None,
            "hits": [],
            "unknown_tags": [],
            "no_coverage": False,
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
            "searched_scope": [],
            "fallback_reason": "no collections registered",
        }

    dense_query = hypothetical or question
    doc = f"vec: {dense_query}\nlex: {question}\n"

    from lies.qmd.cli import QmdCommandError

    try:
        raw = _post_query(doc, scope, limit=10, timeout=15)
    except QmdCommandError as exc:
        return {
            "hit": None,
            "hits": [],
            "unknown_tags": [],
            "no_coverage": True,
            "searched_scope": scope,
            "fallback_reason": f"qmd unreachable: {exc}",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "hit": None,
            "hits": [],
            "unknown_tags": [],
            "no_coverage": True,
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
