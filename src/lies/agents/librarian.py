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
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

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
    """

    collection: str
    slug: str
    title: str
    spans: list[Span]


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

## 1. Classify (registry-driven)

Discover which collections the question touches by reading the LIVE
wiki registry — never a hardcoded map.

1. Call `wiki_catalog` to list every collection. Each entry carries
   `name`, `tags`, and `scope_keywords`.
2. Match the question's tokens against those three surfaces.
3. Build `tag_expr` as a `|`-union of matched tags.
4. When nothing intersects, run UNTAGGED (`tag_expr=None`). An empty
   registry is also untagged; note it in the bundle.
5. Pass the caller's `exclude_tags` through to `wiki_search`
   unchanged so the daemon enforces the exclusion site-side.

## 2. Search

Call `wiki_search` with the question and the `tag_expr` from step 1.
`wiki_search` is the daemon-backed retrieval surface. It resolves
`+tag`/`-tag` tokens internally.

- `unknown_tags` non-empty → hard error: surface it to the caller.
  Do NOT silently retry without tags.
- `no_coverage=True` (or `searched_scope` landed off-domain) → a
  scope miss: consult `searched_scope`, re-read the registry, re-
  scope `tag_expr`, retry once.

## 3. Read (curated excerpts)

Take the top-K hits from `wiki_search`'s `hits` field (highest
`score` first), call `wiki_read` on each, and select the sections
relevant to the question. Return a few hundred words per page —
the verbatim passage that can back a claim — not the whole body.

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


def register_librarian_tools(
    agent: Agent[LibrarianDeps, LibrarianOutput],
    *,
    wiki: Wiki,
    memory_service: WikiMemoryService,
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
    """
    # Local imports — these pull in the catalog reader. The catalog
    # reader sits behind the wiki-side import chain (``pydantic_ai``,
    # ``fastmcp``), so deferring the import keeps the librarian
    # module's cheap import cost. ``RunContext`` and the cast helper
    # are at module scope so pydantic-ai's ``get_type_hints`` resolves
    # the tool function annotations against the module globals.
    from lies.memory.catalog import list_pages as _list_pages
    from lies.memory.catalog import open_catalog as _open_catalog

    def _wiki_search(
        ctx: RunContext[LibrarianDeps],
        question: str,
        limit: int = 5,
    ) -> dict[str, object]:
        """Search this wiki for project knowledge relevant to ``question``.

        Wraps :meth:`WikiMemoryService.search` so the authenticated
        evidence set threads into the librarian's run state.

        F18 Task 1 — also captures the result's ``no_coverage`` flag
        into the module-scope :data:`librarian_no_coverage`
        ContextVar. The dispatch site (orchestrator's ``run_query``)
        reads the ContextVar after ``run_sync`` returns and copies
        the value onto the returned :class:`LibrarianOutput` via
        :func:`dataclasses.replace`. Defaults to ``False`` when the
        underlying ``WikiSearchResult`` doesn't carry the flag yet
        (F18 Task 2/3 surfaces it; Task 1 just plumbs the path).
        """
        result = memory_service.search(question, limit=limit)
        dumped = result.model_dump()
        librarian_no_coverage.set(bool(dumped.get("no_coverage", False)))
        return cast(dict[str, object], dumped)

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
