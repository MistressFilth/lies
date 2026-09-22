"""Librarian subagent — staged retrieval dispatch (F18).

Port of the ask `librarian-prompt.md` 4-step contract (classify →
search → read → return bundle) into a pydantic-ai in-process
subagent. The orchestrator routes retrieval through this agent;
the synthesizer consumes ``LibrarianOutput`` to compose the cited
answer.

Unlike ask's harness-``Agent``-tool dispatch, lies runs in-process
via ``librarian_agent.run_sync(deps)``. No background task, no
host-loop await — the orchestrator's ``run_query`` blocks until
the librarian returns.
"""

from __future__ import annotations

import contextvars
import json
import re
import warnings
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Literal


from pydantic_ai import Agent, RunContext
from pydantic_ai.models import Model

from lies.agents.base import make_sub_agent
from lies.markdown_spans import Span

if TYPE_CHECKING:
    from lies.memory.service import WikiMemoryService
    from lies.wiki.wiki import Wiki


@dataclass(frozen=True)
class LibrarianDeps:
    """Dependencies the librarian subagent needs to retrieve excerpts.

    Attributes:
        question: The user's natural-language question.
        tag_expr: Body of a single include token (no leading sigil),
            e.g. ``'a&b|c'``. ``None`` for untagged queries.
        exclude_tags: NOT tags without leading sigil.
        top_k: Maximum number of excerpts to return (default 5).
    """

    question: str
    tag_expr: str | None
    exclude_tags: list[str]
    top_k: int = 5


@dataclass(frozen=True)
class PageExcerpt:
    """A page excerpt the librarian curated for the question.

    ``spans`` (F19) carries the structured span view rather than a
    raw text blob — the synthesizer picks per-claim span from this
    list.

    ``source_kind`` (dual-source-routing) flags which surface the
    excerpt came from so downstream rendering can distinguish
    primary-source hits (``"library"``) from wiki-only hits
    (``"wiki"``). Library-wins-on-conflict drops the wiki copy on
    slug match, so there is no merged-row third value. Defaults
    to ``"library"`` for backward compat against pre-T2F librarian
    outputs.
    """

    collection: str
    slug: str
    title: str
    spans: list[Span]
    source_kind: Literal["library", "wiki"] = "library"


@dataclass(frozen=True)
class LibrarianOutput:
    """The librarian's evidence bundle — verbatim passages the
    synthesizer composes an answer from.

    The librarian does NOT write the answer; the main agent holds
    the cited synthesis.

    Attributes:
        no_coverage: F18 Task 1 — set by the orchestrator's dispatch
            site from the :data:`librarian_no_coverage` ContextVar
            that ``_wiki_search`` populates. ``True`` when the
            search result carries the flag, signalling a scope miss
            on a populated wiki. Defaults to ``False`` so existing
            ``LibrarianOutput(...)`` construction sites and frozen-
            dataclass consumers stay back-compat.
    """

    tag_expr: str | None
    exclude_tags: list[str]
    excerpts: list[PageExcerpt]
    distinct_pages: int
    no_coverage: bool = False


# F18 Task 1 — module-scope ContextVar that ``_wiki_search``
# populates from the search result's ``no_coverage`` flag and the
# orchestrator's dispatch site copies onto the returned
# :class:`LibrarianOutput`. Default ``False`` so any code path that
# never touches ``_wiki_search`` (canned-answer test fixtures, the
# exception branch in :func:`lies.mcp.grounding.ground`) reports
# coverage rather than raising.
librarian_no_coverage: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "librarian_no_coverage",
    default=False,
)


# Validator workaround — qmd's validateSemanticQuery guard rejects
# in-word hyphens in vec queries. The librarian rewrites compound
# words as multi-word phrases BEFORE posting to qmd.
_HYPHEN_PHRASES: dict[str, str] = {
    "error-handling": "error handling",
    "comma-separated": "comma separated",
    "case-insensitive": "case insensitive",
    "well-defined": "well defined",
    "thread-safe": "thread safe",
}
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_MONTHS = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]


def _rewrite_query_for_validator(query: str) -> str:
    """Rewrite compound words + ISO dates so qmd's vec guard accepts the query.

    The qmd search daemon's ``validateSemanticQuery`` rejects any
    hyphen immediately followed by a word character in vec text as
    suspected negation syntax. This rewrite is conservative — over-
    rewriting is safe; under-rewriting wedges the daemon.

    Args:
        query: The raw question text.

    Returns:
        The query with known compound words expanded and ISO dates
        spelled out.
    """
    out = query
    for hyphenated, expanded in _HYPHEN_PHRASES.items():
        out = out.replace(hyphenated, expanded)
    out = _ISO_DATE_RE.sub(
        lambda m: f"{_MONTHS[int(m.group(2)) - 1]} {int(m.group(3))} {m.group(1)}",
        out,
    )
    return out


LIBRARIAN_SYSTEM_PROMPT = """You are the librarian subagent for the LIES wiki.

Your job is to classify the user's question, retrieve relevant
excerpts, and return a curated evidence bundle. You do NOT write
the answer — the parent turn composes the cited synthesis from
your bundle.

The wiki has TWO retrieval surfaces:
  - **Library collections** (canonical primary sources under
    `library/collections/<name>/`).
  - **Wiki-authored pages** (derived, under `<wiki>/wiki/`).

Both are reachable via the same `wiki_search` tool. Each hit is
tagged with `source_kind` — `"library"` for library hits, `"wiki"`
for wiki-only hits. Library-wins-on-conflict means the wiki copy is
dropped on slug match, so there is no merged-row discriminator.

## 1. Classify (registry-driven, dual-surface)

Discover which collections the question touches by reading the LIVE
registries — never a hardcoded map. Both registries matter; the
library is the primary surface and the wiki is the secondary.

1. Call `wiki_catalog` to list every wiki catalog row. Each entry
   carries `name`, `tags`, and `scope_keywords`.
2. Library collection names are exposed via the addressable-tag
   set the `tag_expr` resolver validates against. Library
   collections live at `library/collections/<name>/`.
3. Match the question's tokens against BOTH surfaces' metadata.
4. Build `tag_expr` as a `|`-union of matched tags. When nothing
   intersects, run UNTAGGED (`tag_expr=None`). An empty registry is
   also untagged; note it in the bundle.
5. Pass the caller's `exclude_tags` through to `wiki_search`
   unchanged so the daemon enforces the exclusion site-side.

## 2. Search (dual-surface)

Call `wiki_search` with the question and the `tag_expr` from
step 1. `wiki_search` is the dual-surface retrieval surface — it
queries BOTH the wiki AND the library collection space, returning
a merged hit list. Library hits are primary; on a slug conflict,
the library hit wins (the wiki hit is dropped from the merged row).

- `unknown_tags` non-empty → hard error: surface it to the caller.
  Do NOT silently retry without tags.
- `no_coverage=True` (or `searched_scope` landed off-domain) → a
  scope miss on the wiki side: consult `searched_scope`, re-read
  the registries, re-scope `tag_expr`, retry once. Library hits
  are unaffected.

## 3. Read (curated excerpts)

Take the top-K hits from `wiki_search`'s `hits` field (highest
`score` first), call `wiki_read` on each, and select the sections
relevant to the question. Return a few hundred words per page —
the verbatim passage that can back a claim — not the whole body.

Each hit carries a `source_kind` tag. Preserve it on the
`PageExcerpt.source_kind` field of every emitted excerpt so
downstream synthesis can mark library-derived claims as primary
and wiki-only claims as secondary.

On a read failure, log and continue with the rest. If every hit
fails, fall back to the `wiki_search` tool and excerpt from its
`pages_read`.

## 4. Return

Emit a JSON evidence bundle:

```json
{
  "tag_expr": "<chosen union, or null when untagged>",
  "exclude_tags": [...],
  "excerpts": [
    {"collection": "<collection>", "slug": "<docid>", "title": "<page title>", "spans": [<Span objects>]}
  ],
  "distinct_pages": <int>
}
```

A claim with no returned excerpt is a signal for the parent turn
to issue a follow-up, not to fabricate.
"""


def librarian_agent(
    model: Model | str | None = None,
) -> Agent[LibrarianDeps, LibrarianOutput]:
    """Construct the librarian subagent.

    Tools: ``wiki_search``, ``wiki_read``, ``wiki_catalog``. Wired by
    :func:`register_librarian_tools` — the bare factory emits an
    ``Agent(tools=[])`` so any caller that omits wiring gets an agent
    with no tools (the F19 ground-digest bug class). See
    :func:`register_librarian_tools` for the canonical wiring path.

    Dispatches via ``run_sync(deps)`` (in-process, not harness
    await-async).
    """
    if model is None:
        from lies.providers.env import env_override
        from lies.errors import ModelNotConfigured

        raise ModelNotConfigured(
            "librarian_agent requires an explicit model. Pass `model=` "
            "or set LIES_AGENT_LIBRARIAN_MODEL / configure providers.toml. "
            f"env_override: {env_override('librarian')!r}"
        )
    agent: Agent[LibrarianDeps, LibrarianOutput] = make_sub_agent(
        model=model,
        output_type=LibrarianOutput,
        deps_type=LibrarianDeps,
        system_prompt=LIBRARIAN_SYSTEM_PROMPT,
    )
    return agent


def _dump_hit(page: Any) -> dict[str, Any]:
    """Coerce one wiki-side ``pages`` row into a hit dict.

    The wiki-side :class:`WikiSearchResult.pages` entries come in
    several shapes — pydantic :class:`WikiEvidence` rows (with
    ``model_dump``), dataclass stubs (with ``__dict__``), or already-
    dumped dicts from ``WikiSearchResult.model_dump()``. Coerce each
    shape to a flat dict while keeping the field set the synthesizer
    already consumes (``page_id``, ``path``, ``collection_id``,
    ``excerpt``, ``score``, ``line_start``, ``line_end``).
    """
    if isinstance(page, dict):
        return dict(page)
    if hasattr(page, "model_dump"):
        return dict(page.model_dump())
    return {k: v for k, v in vars(page).items() if not k.startswith("_")}


def _merge_wiki_and_library_hits(
    wiki_hits: list[dict[str, Any]],
    library_hits: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge wiki + library hits, library-wins-on-slug-conflict.

    Each hit dict carries a ``slug``-equivalent (``path`` for qmd
    hits, ``path`` for wiki hits — both surfaces normalize on the
    collection/path key) used as the dedup key. Library hits are
    merged into a ``{slug: hit}`` map first so any wiki hit on the
    same slug is dropped, then wiki-only hits are appended after.

    Order: library hits first (in their original qmd-returned
    order), then wiki-only hits in wiki-side order. The downstream
    ``LibrarianOutput`` honors top-K, so the order matters — the
    library surface is the primary source of truth.
    """
    merged_by_slug: dict[str, dict[str, Any]] = {}
    library_anonymous: list[dict[str, Any]] = []
    for hit in library_hits:
        slug = str(hit.get("path", ""))
        if not slug:
            # Anonymous library hits have no dedup key — keep them
            # outside the dedup map for reviewability.
            library_anonymous.append(hit)
            continue
        merged_by_slug[slug] = hit
    for hit in wiki_hits:
        slug = str(hit.get("path", ""))
        if not slug:
            continue
        if slug in merged_by_slug:
            # Library wins — drop the wiki copy entirely.
            continue
        merged_by_slug[slug] = hit
    return list(merged_by_slug.values()) + library_anonymous


def resolve_collection_filter(
    *,
    tag_expr: str | None,
    exclude_tags: list[str],
    wiki_name: str,
) -> set[str] | None:
    """Build the qmd-side ``collection_filter`` set from ``LibrarianDeps``.

    Mirrors the MCP ``query`` boundary (``mcp/server.py``): parse the
    include expression, validate against the available tag set, build a
    :class:`ResolvedTagFilter`, resolve to library collection names via
    :func:`lies.query.synthesizer._collections_matching`, and union in
    ``wiki_name`` so the wiki's own qmd collection (``wiki_<name>``)
    keeps participating in the search even when the operator narrowed
    the call to a library collection. Returns ``None`` for an untagged
    librarian run — the qmd CLI receives no filter and the existing
    untagged behavior is preserved.

    Failure modes mirror the MCP boundary:
      - Parse errors / unknown tags raise and bubble up to the agent
        run, surfacing as ``ToolError`` at the MCP layer.
      - Library uninitialized returns an empty set; ``wiki_name`` is
        added so the wiki's qmd collection is the only target.
    """
    from lies.library.registry import library_collection_names
    from lies.query.synthesizer import _collections_matching
    from lies.query.tag_expr import (
        ResolvedTagFilter,
        parse,
        resolve as resolve_tag,
    )

    include_ast = parse(tag_expr) if tag_expr else None
    available = set(library_collection_names())
    if include_ast is not None:
        resolved = resolve_tag(include_ast, available=available)
    else:
        resolved = ResolvedTagFilter(include=None)

    exclude: str | None = None
    exclude_qualifier: str | None = None
    if exclude_tags:
        exclude, exclude_qualifier = _split_exclude_qualifier(exclude_tags[0])

    resolved_with_exclude = ResolvedTagFilter(
        include=resolved.include,
        exclude=exclude,
        exclude_qualifier=exclude_qualifier,
    )

    if resolved_with_exclude.include is None and resolved_with_exclude.exclude is None:
        return None

    matching = _collections_matching(resolved_with_exclude)
    matching.add(wiki_name)
    return matching


def _split_exclude_qualifier(raw: str) -> tuple[str, Literal["t", "c"] | None]:
    """Split a leading ``t:`` / ``c:`` from a raw exclude tag; passthrough otherwise.

    Local mirror of :func:`lies.query.tag_expr.check_qualifier` minus
    the error path — the librarian surfaces unknown excludes via the
    same exclude-passes-through-the-resolver contract the MCP layer
    uses. Errors surface during ``_collections_matching`` instead.
    """
    from lies.query.tag_expr import QUALIFIER_PREFIX_RE

    m = QUALIFIER_PREFIX_RE.match(raw)
    if m is None:
        return raw, None
    qualifier: Literal["t", "c"] = m.group(1)  # ty: ignore[invalid-assignment]
    return m.group(2), qualifier


def register_librarian_tools(
    agent: Agent[LibrarianDeps, LibrarianOutput],
    *,
    wiki: Wiki,
    memory_service: WikiMemoryService,
    qmd_query: "Callable[..., list[dict[str, Any]]] | None" = None,
) -> None:
    """Register ``wiki_search`` / ``wiki_read`` / ``wiki_catalog`` on the librarian agent.

    Tools are defined per the librarian's 4-step contract
    (classify → search → read → return). The closures bind ``wiki``
    and ``memory_service`` so the librarian's deps type
    (:class:`LibrarianDeps` — question / tag_expr / exclude_tags /
    top_k) does not widen to carry the wiki context; the agent's
    typed deps surface stays narrow while the tools see the full
    per-wiki service.

    Extracted from :meth:`Orchestrator._register_librarian_tools`
    so MCP-layer call sites (notably :func:`lies.mcp.grounding.ground`,
    which dispatches in-process rather than via the orchestrator) can
    wire the same tools without instantiating an :class:`Orchestrator`.
    The orchestrator's existing call site continues to delegate to
    this helper so the wiring lives in exactly one place.

    Call once per agent instance after construction. The underlying
    pydantic-ai decorator raises on double-register with the same
    name; callers that own an agent lifecycle (e.g. the orchestrator)
    reuse the existing instance rather than re-wiring.

    The brief's reference to ``wiki_knowledge`` (F19) is left for the
    deeper F19 wiring pass — the F18 trio (search / read / catalog)
    covers the 4-step contract as scoped in this task.

    Args:
        agent: The librarian agent whose toolset to populate.
        wiki: Per-wiki :class:`Wiki` used by the ``wiki_catalog``
            tool.
        memory_service: Per-wiki :class:`WikiMemoryService` that
            backs ``wiki_search`` / ``wiki_read``.
        qmd_query: Library-side qmd helper
            (:func:`lies.qmd.cli.qmd_query` by default). The
            ``_wiki_search`` closure calls this to retrieve the
            library-collection hits; the merged result carries
            ``source_kind`` per hit (``"library"``, ``"wiki"``).
            Tests inject a stub to keep the librarian unit tests
            off the qmd subprocess. ``None`` defaults to the real
            :func:`lies.qmd.cli.qmd_query` helper, lazily imported
            here so the librarian module stays off the qmd import
            chain.
    """
    # Local imports — these pull in the catalog reader. The catalog
    # reader sits behind the wiki-side import chain (``pydantic_ai``,
    # ``fastmcp``), so deferring the import keeps the librarian
    # module's cheap import cost. ``RunContext`` and the cast helper
    # are at module scope so pydantic-ai's ``get_type_hints`` resolves
    # the tool function annotations against the module globals.
    from lies.memory.catalog import list_pages as _list_pages
    from lies.memory.catalog import open_catalog as _open_catalog

    # Dual-source (Task 3): the ``_wiki_search`` closure queries BOTH
    # the wiki (:class:`WikiMemoryService.search`) and the library
    # (qmd index), then merges the two surfaces with library-wins-
    # on-slug-conflict. The qmd helper is a keyword parameter so
    # tests can inject a stub without monkeypatching
    # ``lies.qmd.cli.qmd_query`` globally. When omitted we lazily
    # resolve to the production helper at first call so this module
    # stays off the qmd import chain until needed.
    if qmd_query is None:
        from lies.qmd.cli import qmd_query as _qmd_query_default

        _qmd_query_callable: Callable[..., list[dict[str, Any]]] = _qmd_query_default
    else:
        _qmd_query_callable = qmd_query

    def _wiki_search(
        ctx: RunContext[LibrarianDeps],
        question: str,
        limit: int = 5,
    ) -> dict[str, object]:
        """Search the wiki + library for project knowledge relevant to ``question``.

        Dual-source retrieval (Task 3): queries both the wiki via
        :meth:`WikiMemoryService.search` AND the library collection
        space via qmd, then merges the two surfaces. Each hit carries
        a ``source_kind`` discriminator (``"library"`` for qmd hits,
        ``"wiki"`` for wiki-only hits). On a slug conflict, the
        library hit wins — the merged row carries the library body
        and is tagged ``source_kind="library"`` rather than emitting
        both rows. qmd failures (``QmdError`` subclasses — qmd
        missing, daemon down, etc.) degrade to wiki-only hits; the
        wiki is the source-of-truth and we never lose coverage
        because the qmd path raised.

        F18 Task 1 — also captures the result's ``no_coverage`` flag
        into the module-scope :data:`librarian_no_coverage`
        ContextVar. The dispatch site (orchestrator's ``run_query``)
        reads the ContextVar after ``run_sync`` returns and copies
        the value onto the returned :class:`LibrarianOutput` via
        :func:`dataclasses.replace`. The ContextVar reflects the
        wiki side's signal; library failures do not flip it because a
        live library is not required for coverage.
        """
        from lies.qmd.cli import QmdError

        # Tag-filter plumbing (Fix B from no-default-models-and-
        # phantom-id-fix). The librarian's ``LibrarianDeps.tag_expr``
        # resolves to a qmd-side collection set via the F15 filter;
        # threading it into the wiki-side search scopes both surfaces
        # against the operator's intent.
        collection_filter = resolve_collection_filter(
            tag_expr=ctx.deps.tag_expr,
            exclude_tags=ctx.deps.exclude_tags,
            wiki_name=wiki.name,
        )

        # Wiki side — `WikiSearchResult.model_dump()` produces the
        # real ``{query, pages, truncated, fallback_used,
        # fallback_reason, no_coverage, searched_scope}`` shape.
        # We pluck the wiki hits and re-shape the dict into the
        # canonical ``{hits, no_coverage, searched_scope}`` envelope
        # the rest of the librarian chain already consumes. Keeping
        # the top-level keys stable preserves the F18 test that
        # asserts the dict-shape contract verbatim.
        wiki_result = memory_service.search(
            question, limit=limit, qmd_collection_filter=collection_filter
        )
        wiki_dump = wiki_result.model_dump()
        wiki_pages = wiki_dump.get("pages", [])
        wiki_hits = [{**_dump_hit(page), "source_kind": "wiki"} for page in wiki_pages]
        no_coverage = bool(wiki_dump.get("no_coverage", False))
        searched_scope = list(wiki_dump.get("searched_scope", []))
        librarian_no_coverage.set(no_coverage)

        # Library side — best-effort qmd query. Errors (missing
        # binary, daemon failures, empty result set) collapse to an
        # empty library hit list; the wiki-side merge still wins
        # because library hits are merged by slug rather than
        # short-circuiting the wiki path.
        library_hits: list[dict[str, Any]] = []
        # F15 tag-filter union: every registered library collection
        # is reachable from the librarian's library path. The F15
        # filter is applied post-qmd in `qmd_query` itself.
        try:
            from lies.library.registry import library_collection_names

            _lib_collections = set(library_collection_names())
        except Exception as exc:
            warnings.warn(
                f"librarian: library_collection_names() raised {type(exc).__name__}: {exc}",
                stacklevel=2,
            )
            _lib_collections = None
        try:
            raw_library = _qmd_query_callable(
                wiki.wiki_dir,
                question,
                limit=limit,
                collection_filter=_lib_collections,
            )
        except QmdError:
            raw_library = []
        except Exception as exc:
            # Defensive — qmd wraps most failure modes in
            # :class:`QmdError`, but a totally unexpected exception
            # (subprocess crash, OS error, TypeError from a future
            # qmd change) must not break the librarian's wiki
            # retrieval path. Surface the unexpected error so
            # operators can debug empty-library-hit cases without
            # grep'ing the daemon log.
            warnings.warn(
                f"librarian: library qmd_query raised {type(exc).__name__}: {exc}",
                stacklevel=2,
            )
            raw_library = []

        for hit in raw_library:
            library_hits.append({**hit, "source_kind": "library"})

        # Merge — dedupe by slug. On conflict, the library hit
        # replaces the wiki hit; the merged row carries the library
        # body's title/excerpt/path and the ``source_kind`` is set
        # to ``"library"`` to reflect the winning surface.
        merged = _merge_wiki_and_library_hits(wiki_hits, library_hits)

        return {
            "hits": merged,
            "no_coverage": no_coverage,
            "searched_scope": searched_scope,
        }

    def _wiki_read(
        ctx: RunContext[LibrarianDeps],
        page_ids: list[str],
    ) -> dict[str, str]:
        """Read full page bodies for the given page IDs.

        Thin wrapper around :meth:`WikiMemoryService.read` — IDs
        must already be authenticated by ``wiki_search`` in the
        same run.
        """
        return memory_service.read(page_ids)

    def _wiki_catalog(ctx: RunContext[LibrarianDeps]) -> str:
        """List every wiki catalog row as JSON.

        Mirrors :func:`_wiki_catalog_impl` in the MCP server so
        the librarian has the same registry view the catalog
        MCP resource exposes to the main agent.
        """
        conn = _open_catalog(wiki)
        try:
            pages = _list_pages(conn)
        finally:
            conn.close()
        return json.dumps([p.model_dump(mode="json") for p in pages], indent=2)

    agent.tool(
        name="wiki_search",
        description=(
            "Search this wiki for project knowledge relevant to a question. "
            "Returns bounded evidence with page_id values that can be passed to wiki_read."
        ),
    )(_wiki_search)
    agent.tool(
        name="wiki_read",
        description=(
            "Read the full markdown body of wiki pages identified by page_id. "
            "Accepts only IDs returned by a recent wiki_search call."
        ),
    )(_wiki_read)
    agent.tool(
        name="wiki_catalog",
        description=(
            "List every wiki catalog row as JSON. Each entry carries name, tags, "
            "scope_keywords, and section. Use this for the classify step in the 4-step contract."
        ),
    )(_wiki_catalog)
