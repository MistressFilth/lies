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
        exclude_expr: Compiled exclude AST (Task 3 /
            f15-exclude-compound). ``None`` when no ``-`` chain was
            supplied. The dep carries the AST rather than a flat
            ``list[str]`` because the F15 grammar now accepts
            compound excludes (``-c:foo&c:bar``, ``-c:foo|c:bar``)
            whose per-collection dispatch walks the tree. The
            historical ``list[str]`` contract was retired in Task 3
            along with the flat-string ``ResolvedTagFilter.exclude``
            field.
        top_k: Maximum number of excerpts to return (default 5).
    """

    question: str
    tag_expr: str | None
    exclude_expr: Any  # TagExpr | None AST (Task 3); Any at runtime so
    # pydantic-ai's TypeAdapter doesn't try to build a schema for
    # TagExpr (a stdlib @dataclass, not pydantic).
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
    exclude_expr: Any  # TagExpr | None AST (Task 3); Any at runtime so
    # pydantic-ai's TypeAdapter doesn't try to build a schema for
    # TagExpr (a stdlib @dataclass, not pydantic).
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
5. The caller's `exclude_expr` is a compiled `TagExpr` AST (Task 3
   / f15-exclude-compound). Do NOT pass it to `wiki_search` —
   exclusion is applied site-side by `_wiki_search` against each
   hit's collection first-segment before the merged list is
   returned. Just call `wiki_search` with the question and
   `tag_expr`; the tool enforces the exclude AST internally and
   the LLM never sees an `exclude_expr` kwarg. (`wiki_search` does
   accept `exclude_expr` for callers that want to apply exclusion
   directly — the librarian's own calls leave it at the default
   `None`.)

## 2. Search (dual-surface)

Call `wiki_search` with the question and the `tag_expr` from
step 1. `wiki_search` is the dual-surface retrieval surface — it
queries BOTH the wiki AND the library collection space, returning
a merged hit list. Library hits are primary; on a slug conflict,
the library hit wins (the wiki hit is dropped from the merged row).

`_wiki_search` queries two surfaces in sequence: the wiki's qmd
index at `wiki.wiki_dir` AND the library's qmd index at
`lib.git_root` (with `collection_filter` of registered library
collection names). Each hit carries `source_kind` (`"library"` for
qmd-library hits, `"wiki"` for wiki hits). On slug conflict the
library hit wins.

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
  "exclude_expr": "<compiled TagExpr AST or null>",
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
    model: Model | str = "anthropic:claude-opus-4-7",
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


def register_librarian_tools(
    agent: Agent[LibrarianDeps, LibrarianOutput],
    *,
    wiki: Wiki,
    memory_service: WikiMemoryService,
    qmd_query: "Callable[..., list[dict[str, Any]]] | None" = None,
    qmd_get: "Callable[..., str] | None" = None,
) -> None:
    """Register ``wiki_search`` / ``wiki_read`` / ``wiki_catalog`` on the librarian agent.

    Tools are defined per the librarian's 4-step contract
    (classify → search → read → return). The closures bind ``wiki``
    and ``memory_service`` so the librarian's deps type
    (:class:`LibrarianDeps` — question / tag_expr / exclude_expr /
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

    # Task B - source-aware ``_wiki_read`` dispatch reads library-side
    # bodies via ``qmd get`` against ``library_git_root()``. The
    # helper is a keyword parameter so tests can inject a stub without
    # monkeypatching ``lies.qmd.cli.qmd_get`` globally. When omitted
    # we lazily resolve to the production helper at first call so this
    # module stays off the qmd import chain until needed.
    if qmd_get is None:
        from lies.qmd.cli import qmd_get as _qmd_get_default

        _qmd_get_callable: Callable[..., str] = _qmd_get_default
    else:
        _qmd_get_callable = qmd_get

    def _wiki_search(
        ctx: RunContext[LibrarianDeps],
        question: str,
        limit: int = 5,
        exclude_expr: Any = None,  # TagExpr | None AST (Task 3);
        # Any at runtime so pydantic-ai's tool schema generator
        # doesn't try to build a JSON schema for the stdlib
        # ``TagExpr`` dataclass — same workaround as
        # :class:`LibrarianDeps.exclude_expr`.
    ) -> dict[str, object]:
        """Search the wiki + library for project knowledge relevant to ``question``.

        Dual-source retrieval: queries BOTH surfaces via qmd in
        sequence, then merges the two surfaces. Each hit carries a
        ``source_kind`` discriminator (``"library"`` for qmd-library
        hits, ``"wiki"`` for qmd-wiki hits). On a slug conflict, the
        library hit wins — the merged row carries the library body
        and is tagged ``source_kind="library"`` rather than emitting
        both rows.

        The library's qmd index is registered at ``lib.git_root``
        (``~/.local/share/lies/library/``), NOT at any wiki's
        ``wiki_dir``. The fan-out therefore calls qmd against TWO
        cwds in sequence: ``wiki.wiki_dir`` (no filter) for the wiki
        side, ``library_git_root()`` with
        ``collection_filter=set(library_collection_names())`` for the
        library side. A single qmd call against the wiki cwd with a
        library-collection filter would return zero library hits
        because the wiki's qmd index has no library pages registered.

        The wiki-side ``no_coverage`` and ``searched_scope`` signals
        still come from :meth:`WikiMemoryService.search` because qmd
        has no equivalent: it indexes pages, not corpus-coverage
        metadata. Library-side failures do not flip ``no_coverage``
        because a live library is not required for coverage.

        qmd failures (``QmdError`` subclasses — qmd missing, daemon
        down, etc.) degrade to empty result lists on the affected
        side; the other side's hits still reach the merged output,
        so coverage is never lost because one path raised.

        F18 Task 1 — also captures the result's ``no_coverage`` flag
        into the module-scope :data:`librarian_no_coverage`
        ContextVar. The dispatch site (orchestrator's ``run_query``)
        reads the ContextVar after ``run_sync`` returns and copies
        the value onto the returned :class:`LibrarianOutput` via
        :func:`dataclasses.replace`. The ContextVar reflects the
        wiki side's signal; library failures do not flip it because a
        live library is not required for coverage.

        Site-side exclusion (Task F / urgent-bug-f): when
        ``exclude_expr`` is a non-``None`` compiled ``TagExpr`` AST
        (the same shape ``LibrarianDeps.exclude_expr`` carries), the
        merged hit list is filtered against it before being
        returned. Each hit's collection first-segment is fed to
        :func:`lies.query.tag_expr.exclude_matches` via a synthesized
        :class:`LibraryCollectionMeta(name=first_seg, tags=())`. For
        library hits, ``first_seg`` is the library collection name
        (``opencode``, ``claude_platform``, …); for wiki hits, it is
        the wiki synthesis namespace (``default`` or whatever the
        wiki is named). A hit whose synthesized record matches the
        AST is dropped. Hits with no path / no first-segment are
        kept rather than silently dropped — matches the library
        retriever's "tagless hits are not excluded" semantics.
        """
        from lies.qmd.cli import QmdError

        # Wiki side — qmd query against the wiki's own qmd index at
        # ``wiki.wiki_dir``. The wiki corpus is registered as a qmd
        # collection rooted at ``wiki.wiki_dir``; this is the
        # canonical way to surface wiki-authored pages alongside
        # library pages in a single merged result list. NO
        # collection_filter — the wiki side is unconstrained
        # because the wiki's qmd index only contains wiki pages.
        #
        # ``no_coverage`` and ``searched_scope`` still come from
        # :meth:`WikiMemoryService.search` because qmd has no
        # equivalent: it indexes pages, not corpus-coverage
        # metadata. The qmd call below is the hit source; the
        # memory-service call supplies the envelope fields the
        # orchestrator copies onto the returned ``LibrarianOutput``.
        try:
            raw_wiki = _qmd_query_callable(
                wiki.wiki_dir,
                question,
                limit=limit,
            )
        except QmdError:
            raw_wiki = []
        except Exception as exc:
            # Defensive — qmd wraps most failure modes in
            # :class:`QmdError`, but a totally unexpected exception
            # (subprocess crash, OS error, TypeError from a future
            # qmd change) must not break the librarian's wiki
            # retrieval path. Surface the unexpected error so
            # operators can debug empty-wiki-hit cases without
            # grep'ing the daemon log.
            warnings.warn(
                f"librarian: wiki qmd_query raised {type(exc).__name__}: {exc}",
                stacklevel=2,
            )
            raw_wiki = []

        # F18 Task 1 — capture the wiki-side ``no_coverage`` and
        # ``searched_scope`` signals into the module-scope
        # :data:`librarian_no_coverage` ContextVar and the result
        # envelope. The dispatch site (orchestrator's ``run_query``)
        # reads the ContextVar after ``run_sync`` returns and copies
        # the value onto the returned :class:`LibrarianOutput` via
        # :func:`dataclasses.replace`. The ContextVar reflects the
        # wiki side's signal; library failures do not flip it because
        # a live library is not required for coverage.
        #
        # Moved BEFORE the wiki-hit assembly below so we can build
        # the ``path -> page_id`` lookup the assembly needs to
        # convert qmd docids (``#abc123``) into the wiki ``page-`` +
        # sha1-12 IDs that ``_wiki_read`` (and
        # ``memory_service.read``) understand. ``memory_service.search``
        # returns real wiki pages with the canonical wiki page_ids;
        # qmd returns hits with its own docid format. Without the
        # conversion, ``_wiki_read(['#d75430'])`` raises
        # ``WikiPageNotFound`` because ``#abc123`` matches neither
        # the ``page-`` prefix nor a ``/`` separator.
        wiki_envelope = memory_service.search(question, limit=limit)
        wiki_dump = wiki_envelope.model_dump()
        no_coverage = bool(wiki_dump.get("no_coverage", False))
        searched_scope = list(wiki_dump.get("searched_scope", []))
        librarian_no_coverage.set(no_coverage)

        # Build ``path -> wiki page_id`` from memory_service.search's
        # results. Each ``WikiEvidence`` carries the wiki's canonical
        # ``page-`` + sha1-12 ID; the qmd hits carry ``#abc123``-style
        # docids that ``_wiki_read`` cannot resolve. Mapping by path
        # is best-effort: a qmd hit whose path doesn't match any wiki
        # search hit falls through to ``page_id=None`` (library-style
        # contract) so the LLM agent skips ``wiki_read`` instead of
        # passing a qmd docid.
        path_to_wiki_id: dict[str, str] = {
            page["path"]: page["page_id"]
            for page in wiki_dump.get("pages", [])
            if page.get("path") and page.get("page_id")
        }

        wiki_hits: list[dict[str, Any]] = []
        for hit in raw_wiki:
            hit_path = hit.get("path", "")
            wiki_page_id = path_to_wiki_id.get(hit_path)
            # Strip qmd's foreign identifiers (``page_id`` and
            # ``docid``) so the LLM agent never sees a qmd-style
            # identifier in the search results. ``_wiki_read`` only
            # recognizes wiki ``page-`` + sha1-12 IDs and library
            # paths (``<collection>/<page>``); a qmd docid like
            # ``#d75430`` matches neither prefix nor separator and
            # would otherwise raise ``WikiPageNotFound`` if the LLM
            # extracted it and passed it back to ``wiki_read``.
            # Stripping both fields covers the qmd versions where
            # the docid is named ``docid`` instead of ``page_id``
            # (Fix-D2-extend). The wiki ``page-`` + sha1-12 ID is
            # supplied via the mapped ``wiki_page_id`` below;
            # ``wiki_page_id`` is ``None`` when the qmd hit doesn't
            # match any wiki search hit (best-effort fallback;
            # mirrors the library-side contract).
            clean = {k: v for k, v in hit.items() if k not in ("page_id", "docid")}
            wiki_hits.append(
                {
                    **clean,
                    "page_id": wiki_page_id,
                    "source_kind": "wiki",
                }
            )

        # Library side — best-effort qmd query against the library's
        # own qmd index. Errors (missing binary, daemon failures,
        # empty result set) collapse to an empty library hit list;
        # the wiki-side merge still wins because library hits are
        # merged by slug rather than short-circuiting the wiki path.
        #
        # The library's qmd index is registered at ``lib.git_root``,
        # NOT at any wiki's ``wiki_dir`` — the two surfaces are
        # distinct indexes. Calling qmd against ``wiki.wiki_dir``
        # returns zero library hits even when the library is
        # populated. The dual-source fan-out therefore queries
        # BOTH surfaces in sequence and merges the two result
        # lists via ``_merge_wiki_and_library_hits``, which enforces
        # library-wins-on-slug-conflict.
        library_hits: list[dict[str, Any]] = []
        # F15 tag-filter union: every registered library collection
        # is reachable from the librarian's library path. The F15
        # filter is applied post-qmd in `qmd_query` itself. The set
        # of registered names comes from the live library registry
        # (NOT the wiki catalog) so the filter is the source of
        # truth for which collection names qmd can return.
        try:
            from lies.library.registry import (
                library_collection_names,
                library_git_root,
            )

            _lib_root = library_git_root()
            _lib_collections: set[str] | None = set(library_collection_names())
        except Exception as exc:
            warnings.warn(
                f"librarian: library registry lookup raised {type(exc).__name__}: {exc}",
                stacklevel=2,
            )
            _lib_root = None
            _lib_collections = None
        if _lib_root is not None:
            try:
                raw_library = _qmd_query_callable(
                    _lib_root,
                    question,
                    limit=limit,
                    collection_filter=_lib_collections,
                )
            except QmdError:
                raw_library = []
            except Exception as exc:
                # Defensive — qmd wraps most failure modes in
                # :class:`QmdError`, but a totally unexpected exception
                # (subprocess crash, OS error, TypeError from a
                # future qmd change) must not break the librarian's
                # wiki retrieval path. Surface the unexpected error
                # so operators can debug empty-library-hit cases
                # without grep'ing the daemon log.
                warnings.warn(
                    f"librarian: library qmd_query raised {type(exc).__name__}: {exc}",
                    stacklevel=2,
                )
                raw_library = []
        else:
            raw_library = []

        for hit in raw_library:
            # Strip qmd's foreign identifiers (``page_id`` and
            # ``docid``) from library hits. Library content is already
            # inlined into the hit body at qmd search time, so the LLM
            # agent does not need to call ``wiki_read`` for library
            # hits - and ``wiki_read`` only knows wiki ``page-`` +
            # sha1-12 IDs, so a qmd docid like ``#abc123`` would
            # otherwise raise ``WikiPageNotFound``. Stripping both
            # fields covers the qmd versions where the docid is named
            # ``docid`` instead of ``page_id`` (Fix-D2-extend). Setting
            # ``page_id=None`` here is the contract that tells the LLM
            # agent to skip the read step for library hits entirely.
            clean = {k: v for k, v in hit.items() if k not in ("page_id", "docid")}
            library_hits.append({**clean, "page_id": None, "source_kind": "library"})

        # Merge — dedupe by slug. On conflict, the library hit
        # replaces the wiki hit; the merged row carries the library
        # body's title/excerpt/path and the ``source_kind`` is set
        # to ``"library"`` to reflect the winning surface.
        merged = _merge_wiki_and_library_hits(wiki_hits, library_hits)

        # Site-side exclude filter (Task F / urgent-bug-f). When the
        # caller passes a compiled ``TagExpr`` AST, drop any hit
        # whose collection first-segment matches. Library hits:
        # ``first_seg`` IS the library collection name (``opencode``,
        # ``claude_platform``, …). Wiki hits: ``first_seg`` is the
        # wiki synthesis namespace (``default`` or whatever); we
        # evaluate that namespace against the exclude AST too so an
        # exclude targeting the namespace drops wiki hits carrying
        # it. Both surfaces feed the matcher via a synthesized
        # :class:`LibraryCollectionMeta(name=first_seg, tags=())`
        # because :func:`exclude_matches` only reads ``.name`` and
        # ``.tags``. Hits with no path / no first-segment are kept
        # rather than silently dropped — matches the library
        # retriever's "tagless hits are not excluded" semantics.
        if exclude_expr is not None:
            from lies.library.registry import LibraryCollectionMeta
            from lies.query.tag_expr import exclude_matches

            filtered: list[dict[str, Any]] = []
            for hit in merged:
                path = str(hit.get("path", ""))
                first_seg = path.split("/", 1)[0] if path else ""
                if not first_seg:
                    filtered.append(hit)
                    continue
                coll = LibraryCollectionMeta(name=first_seg, tags=())
                if exclude_matches(coll, exclude_expr):
                    continue
                filtered.append(hit)
            merged = filtered

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

        Source-aware dispatch (Task B):

        - Wiki page IDs (``page-`` + sha1-12) ->
          ``memory_service.read()``.
        - Library collection paths (``<collection>/<page>``) ->
          read from the library's qmd chunks via ``qmd get``.
        - Unknown format / not found -> ``WikiPageNotFound``.

        The librarian's LLM agent passes either wiki page IDs or
        library paths based on the ``source_kind`` of the hit it
        wants to read. Library hits carry ``page_id=None`` (set at
        search time) so the LLM does not pass qmd docids here;
        this dispatch is the fallback for the case where the LLM
        still routes a library path through ``wiki_read`` instead
        of reading the body it already has in the search result.
        """
        from lies.qmd.cli import QmdError
        from lies.library.registry import library_git_root

        bodies: dict[str, str] = {}
        unknown: list[str] = []
        wiki_ids: list[str] = []
        # Library dispatch pairs each raw input pid (body dict key)
        # with the bare library path (qmd_get URI argument). This
        # preserves the existing "key by input pid" semantics while
        # letting the qmd_get call drop any ``qmd://`` prefix that
        # the caller included on the input.
        library_dispatch: list[tuple[str, str]] = []
        for pid in page_ids:
            if pid.startswith("page-"):
                wiki_ids.append(pid)
                continue
            # qmd's own search results surface library paths as
            # ``qmd://<collection>/<page>`` (the same URI form qmd_get
            # accepts). Strip the prefix before classification so the
            # ``/`` check below doesn't capture it AND so the qmd_get
            # call doesn't double-prefix into ``qmd://qmd://...``.
            bare = pid.removeprefix("qmd://") if pid.startswith("qmd://") else pid
            if "/" in bare:
                library_dispatch.append((pid, bare))
            else:
                unknown.append(pid)
        if wiki_ids:
            bodies.update(memory_service.read(wiki_ids))
        lib_root = library_git_root()
        for raw_pid, bare_path in library_dispatch:
            try:
                bodies[raw_pid] = _qmd_get_callable(lib_root, f"qmd://{bare_path}")
            except QmdError:
                unknown.append(raw_pid)
        if unknown:
            from lies.memory.models import WikiPageNotFound

            raise WikiPageNotFound(f"unknown page_ids: {unknown}")
        return bodies

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
            "Accepts an optional ``exclude_expr`` (a compiled ``TagExpr`` "
            "AST) — when present, hits whose collection first-segment "
            "matches the exclude AST are dropped site-side before being "
            "returned. Returns bounded evidence with page_id values that "
            "can be passed to wiki_read."
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
