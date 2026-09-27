"""Librarian subagent — staged retrieval dispatch (v0.40 port).

Library-mode rewrite of the F18 librarian. The new contract has the
LLM run a 4-step pipeline:

  1. **Classify** — call ``collections_read("list")``, build a
     ``tag_expr`` body from registry tokens (no hardcoded map).
  2. **Search** — call ``search(question, tag_expr, exclude_tags)``
     against the registered library corpus (single-batch hybrid
     vec+lex qmd query). The result is a ``SearchResult`` envelope
     with snippets attached.
  3. **Read** (snippet-review decides) — the LLM reviews each hit's
     snippet, picks the 3-5 paths whose snippets actually cover the
     question, and calls ``read(picked_paths)`` for verbatim bodies.
  4. **Return** — package the curator's excerpts into
     :class:`LibrarianOutput`.

Step 3 is the structural fix for the v0.39 bug class: the librarian
LLM now sees snippets before committing to reads, so authoring pages
rank above install / overview / localized pages.

Unlike ask's harness-``Agent``-tool dispatch, lies runs in-process
via ``librarian_agent.run_sync(deps)``. No background task, no
host-loop await — the orchestrator's ``run_query`` blocks until the
librarian returns.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

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
        no_coverage: v0.40 — populated by the LLM from the ``search``
            MCP tool's ``no_coverage`` field in the SearchResult
            envelope. ``True`` when the corpus has zero hits for the
            question (a scope miss on a populated library). The
            orchestrator's dispatch site passes the field through
            unchanged; downstream consumers (the file-back gate,
            ``ground()`` dispatch, callers reading the bundle) read
            the value as the agent emitted it. Defaults to ``False``
            so existing ``LibrarianOutput(...)`` construction sites
            and frozen-dataclass consumers stay back-compat.
        searched_scope: v0.40 additive — the resolved collection
            list from Step 2's ``search()`` tool
            (``searched_scope`` field). ``MCP synthesize`` threads
            this onto :class:`SynthesizeEnvelope.searched_scope`
            so the CLI / MCP layer can render the scope envelope
            without re-resolving the AST. Defaults to ``[]`` so
            pre-v0.40 construction sites stay back-compat.
    """

    tag_expr: str | None
    exclude_expr: Any  # TagExpr | None AST (Task 3); Any at runtime so
    # pydantic-ai's TypeAdapter doesn't try to build a schema for
    # TagExpr (a stdlib @dataclass, not pydantic).
    excerpts: list[PageExcerpt]
    distinct_pages: int
    no_coverage: bool = False
    searched_scope: list[str] = field(default_factory=list)


LIBRARIAN_SYSTEM_PROMPT = """# librarian — Classify, Search, Read, Return

You are the librarian subagent. Your job is to surface verbatim
passages that answer the user's question. You NEVER write the
answer; the parent synthesizer does.

The corpus is a set of library collections registered under
`$LIES_XDG_DATA_HOME/lies/library/collections/<name>/`. Each
collection carries `name`, `tags`, and `scope_keywords` metadata.
There is no per-wiki mirror in v0.40 — the library is the source of
truth for retrieval.

## 1. Classify (registry-driven)

Discover which collections the question touches by reading the LIVE
registry — never a hardcoded map.

1. Call `collections_read` with `subcommand="list"`. Each entry
   carries `name`, `tags`, and `scope_keywords`.
2. When the tag vocabulary is ambiguous, `subcommand="tag_list"`
   returns a compact `tag → collections` map to disambiguate.
3. Match the question's tokens against those three surfaces.
4. Build a `tag_expr` body (no leading sigil) using `c:<name>` for
   collection-qualified atoms and `|` for OR. Examples:
   - a single collection match → `c:opencode`
   - two collection matches → `c:opencode|c:claude_code`
   - no matches → untagged (`tag_expr=None`)
5. The user's caller-supplied filter (e.g. `+c:opencode|c:claude_code`)
   is a CONSTRAINT — intersect, do not replace. If the caller passed
   `+c:opencode|c:claude_code` and the registry only matches
   `opencode`, use `c:opencode`.
6. When nothing intersects, run UNTAGGED (`tag_expr=None`). An empty
   registry is also untagged; note it in the bundle.

## 2. Search

Call `search(question, tag_expr, exclude_tags)` with the `tag_expr`
from step 1. `search` is a single-batch hybrid vec+lex qmd query
that returns a `SearchResult` envelope:
`{hit, hits, unknown_tags, no_coverage, searched_scope, fallback_reason}`.

`hits` is a list of `{path, title, score, snippet}` rows; each
snippet is a ~200-char window around the matched line in the source
markdown.

- `unknown_tags` non-empty → return an empty bundle with the
  unknown spec in the envelope. Do NOT silently retry without tags.
- `no_coverage=True` → return an empty bundle; the corpus has zero
  hits for this question.
- `fallback_reason` non-None → the daemon errored; return an empty
  bundle with `no_coverage=True` and surface the reason in the
  envelope metadata.

`searched_scope` is the resolved collection list — mirror it
verbatim onto `LibrarianOutput.searched_scope` so the synthesizer
and CLI can render the scope envelope without re-resolving.

### Validator workaround — in-word hyphens in `vec` queries

The search daemon's `validateSemanticQuery` guard rejects any
hyphen immediately followed by a word character in `vec` text as
suspected negation syntax. When composing the question string,
rewrite compound words as multi-word phrases:

| Instead of | Write |
|------------|-------|
| `error-handling` | `error handling` |
| `comma-separated` | `comma separated` |
| `case-insensitive` | `case insensitive` |
| `well-defined` | `well defined` |
| `thread-safe` | `thread safe` |
| `2026-05-10` | `May 10 2026` (or omit the date) |

Over-rewriting is safe; under-rewriting wedges the daemon.

## 3. Read (snippet-review decides — the Snippet step)

For each hit in `hits`, READ THE SNIPPET. The snippet shows ~200
chars around the matched line — enough to know if the page covers
what the user asked for. Do NOT call `read` for every hit; that
blows the context budget and surfaces pages whose snippets already
ruled them out.

PICK the hits whose snippets are most relevant to the question's
intent. Prefer authoring content over install / overview content.
Prefer specificity over breadth. Usually 3–5 reads.

Call `read(picked_paths)` where `picked_paths` is the list of paths
you chose. The result is a dict `{path: body}`. Per-path failures
are skipped + logged inside the tool; if every read fails, fall
back to the snippets already attached to the search hits (Step 2's
`hits[*].snippet`).

The snippet-review step is the structural fix for the v0.39 bug
class: the librarian LLM sees the snippet first, so authoring pages
rank above install / overview / localized pages.

## 4. Return

Emit a `LibrarianOutput` with:

- `tag_expr` — the resolved union from Step 1 (or `None` when
  untagged).
- `exclude_expr` — the caller's compiled `TagExpr` AST (the same
  shape `LibrarianDeps.exclude_expr` carries), or `None`.
- `excerpts` — list of `PageExcerpt` rows. Each row carries the
  curated verbatim passage (a few hundred words), broken into
  `Span` objects with `heading_path` for per-claim threading.

  ```python
  PageExcerpt(
      collection="<first_seg_of_path>",
      slug="<path>",
      title="<title>",
      spans=[Span(heading_path=[...], body="<verbatim excerpt>", ...)],
      source_kind="library",
  )
  ```

- `distinct_pages` — count of distinct paths read.
- `no_coverage` — `True` when every read failed or `hits` was
  empty.
- `searched_scope` — mirror the search tool's `searched_scope`
  field verbatim (the resolved collection list).

```python
{
  "tag_expr": "<resolved union, or null when untagged>",
  "exclude_expr": null,
  "excerpts": [
    {"collection": "<first_seg>", "slug": "<path>", "title": "<title>",
     "spans": [<Span objects>]}
  ],
  "distinct_pages": <int>,
  "no_coverage": <bool>,
  "searched_scope": [...]
}
```

A claim with no returned excerpt is a signal for the parent turn
to issue a follow-up, not to fabricate.

## 5. Output format (MANDATORY)

Your final assistant message MUST be a single JSON object that
matches the :class:`LibrarianOutput` schema below — nothing else.

- Do NOT emit prose, narration, thinking, or commentary.
- Do NOT emit a leading phrase like "Confirmed —" or "I have enough
  material to …". Step 4's bundle IS your final message.
- Do NOT echo the question or restate the tool calls.
- Do NOT wrap the JSON in markdown fences (e.g. ` ```json ... ``` `);
  emit the bare JSON object so pydantic-ai's output validator can
  parse it.
- When the corpus has no hits, the JSON is still required: emit
  ``{"tag_expr": …, "exclude_expr": null, "excerpts": [],
  "distinct_pages": 0, "no_coverage": true, "searched_scope": …}``.
  An empty ``excerpts`` list with ``no_coverage: true`` is the
  expected shape, not a failure to comply.

Concrete schema (the validator expects exactly this JSON shape):

```json
{
  "tag_expr": "c:opencode|c:claude_code",
  "exclude_expr": null,
  "excerpts": [
    {
      "collection": "opencode",
      "slug": "opencode/plugins/authoring.md",
      "title": "Plugin authoring",
      "spans": [
        {
          "heading_path": ["Authoring", "Lifecycle"],
          "body": "verbatim passage from the page body",
          "code_fence": false,
          "start_line": 42
        }
      ],
      "source_kind": "library"
    }
  ],
  "distinct_pages": 1,
  "no_coverage": false,
  "searched_scope": ["opencode"]
}
```

The `exclude_expr` field carries a compiled AST (not a string);
emit ``null`` when no ``-`` chain was supplied. ``source_kind``
is always ``"library"`` for hits served from the library corpus
and ``"wiki"`` for hits served from a registered wiki (latter is
rare in v0.40 — the library is the source of truth).

If pydantic-ai rejects your output, the agent re-prompts you with
the validator's error; on the next attempt, emit ONLY the corrected
JSON — no preamble, no apology, no recap of the failed attempt.
"""


def librarian_agent(
    model: Model | str | None = None,
) -> Agent[LibrarianDeps, LibrarianOutput]:
    """Construct the librarian subagent.

    Tools: ``collections_read``, ``search``, ``read``. Wired by
    :func:`register_librarian_tools` — the bare factory emits an
    ``Agent(tools=[])`` so any caller that omits wiring gets an agent
    with no tools (the F19 ground-digest bug class). See
    :func:`register_librarian_tools` for the canonical wiring path.

    Dispatches via ``run_sync(deps)`` (in-process, not harness
    await-async).
    """
    if model is None:
        from lies.errors import ModelNotConfigured

        raise ModelNotConfigured(
            "librarian_agent requires an explicit model. Pass `model=` "
            "or set LIES_AGENT_LIBRARIAN_MODEL / configure providers.toml."
        )
    agent: Agent[LibrarianDeps, LibrarianOutput] = make_sub_agent(
        model=model,
        output_type=LibrarianOutput,
        deps_type=LibrarianDeps,
        system_prompt=LIBRARIAN_SYSTEM_PROMPT,
        output_retries=3,
    )
    return agent


def _call_tool(tool: Any, **kwargs: Any) -> Any:
    """Invoke a FastMCP tool, supporting test patches.

    Production: ``tool`` is a :class:`fastmcp.tools.function_tool.FunctionTool`
    whose underlying callable is ``tool.fn``. Tests patch the module
    attribute (``lies.mcp.search.search``) with a plain lambda or
    :class:`MagicMock` that has no ``.fn`` attribute. This helper
    prefers the canonical ``.fn`` path and falls back to a direct
    call so both shapes work without the closure having to know
    which one it is.
    """
    if hasattr(tool, "fn") and callable(tool.fn):
        return tool.fn(**kwargs)
    return tool(**kwargs)


def register_librarian_tools(
    agent: Agent[LibrarianDeps, LibrarianOutput],
    *,
    wiki: "Wiki | None" = None,
    memory_service: "WikiMemoryService | None" = None,
    qmd_query: Any = None,
    qmd_get: Any = None,
) -> None:
    """Register ``collections_read`` / ``search`` / ``read`` on the librarian agent.

    Tools are defined per the librarian's 4-step contract
    (classify → search → read → return). Each closure is a thin
    adapter to the corresponding MCP tool at
    ``lies.mcp.{collections,search,read}.{fn}`` — the librarian
    does NOT carry its own qmd / memory-service coupling in v0.40
    (the library corpus is the single source of truth for
    retrieval).

    The ``wiki`` / ``memory_service`` / ``qmd_query`` / ``qmd_get``
    parameters are preserved for backward compatibility with the
    orchestrator's :meth:`Orchestrator._register_librarian_tools`
    call site and the :func:`lies.mcp.grounding.ground` wiring
    path. They are accepted-but-ignored in v0.40 because the new
    tool set is stateless (delegates to MCP tools). Task 9 rewires
    the production call sites to drop these kwargs; this signature
    keeps the v0.39 callers compiling until that work lands.

    Call once per agent instance after construction. The underlying
    pydantic-ai decorator raises on double-register with the same
    name; callers that own an agent lifecycle (e.g. the orchestrator)
    reuse the existing instance rather than re-wiring.

    Args:
        agent: The librarian agent whose toolset to populate.
        wiki: Accepted for back-compat with the F18 call site; unused.
        memory_service: Accepted for back-compat; unused.
        qmd_query: Accepted for back-compat; unused.
        qmd_get: Accepted for back-compat; unused.
    """
    del wiki, memory_service, qmd_query, qmd_get  # back-compat: v0.39 callers

    def _collections_read(
        ctx: RunContext[LibrarianDeps],
        subcommand: Literal["list", "tag_list", "info"],
        name: str | None = None,
    ) -> list[dict[str, Any]] | dict[str, Any]:
        """Read the live library registry.

        Subcommands:
        - ``list`` returns one row per registered collection.
        - ``tag_list`` returns a ``tag → collections`` map.
        - ``info`` + ``name=…`` returns a single collection's full
          metadata.

        Step 1 of the 4-step pipeline. The librarian calls this to
        discover which collections the question touches before
        building ``tag_expr``.
        """
        from lies.mcp import collections as _collections_mod

        return _collections_mod.collections_read(subcommand, name=name)

    def _search(
        ctx: RunContext[LibrarianDeps],
        question: str,
        tag_expr: str | None = None,
        exclude_tags: list[str] | None = None,
        hypothetical: str | None = None,
    ) -> dict[str, Any]:
        """Run a single-batch hybrid vec+lex qmd query.

        Step 2 of the 4-step pipeline. Returns a
        :class:`SearchResult` envelope:
        ``{hit, hits, unknown_tags, no_coverage, searched_scope,
        fallback_reason}``. ``hits`` is a list of
        ``{path, title, score, snippet}`` rows; the snippet is the
        matched line's ~200-char window. The LLM uses the snippet
        to decide which hits to read in Step 3.
        """
        from lies.mcp import search as _search_mod

        return _call_tool(
            _search_mod.search,
            question=question,
            tag_expr=tag_expr,
            exclude_tags=exclude_tags,
            hypothetical=hypothetical,
        )

    def _read(
        ctx: RunContext[LibrarianDeps],
        paths: list[str],
    ) -> dict[str, str]:
        """Read verbatim bodies for the given paths.

        Step 3 of the 4-step pipeline. ``paths`` is the curator's
        picked subset (3-5 paths whose snippets were most relevant).
        Returns ``{path: body}``; per-path failures are logged +
        skipped inside the tool. Library dispatch reads via
        ``qmd_get`` against the library's git root; wiki-page-id
        dispatch reads via the active wiki's
        :class:`WikiMemoryService`.
        """
        from lies.mcp import read as _read_mod

        return _call_tool(_read_mod.read, paths=paths)

    agent.tool(
        name="collections_read",
        description=(
            "Read the live library registry. Subcommands: 'list' "
            "returns one row per registered collection (name, tags, "
            "scope_keywords); 'tag_list' returns a tag → collections "
            "map; 'info' + name returns a single collection's full "
            "metadata. Use 'list' first to discover which collections "
            "the question touches before building tag_expr."
        ),
    )(_collections_read)
    agent.tool(
        name="search",
        description=(
            "Run a single-batch hybrid vec+lex qmd query against the "
            "library corpus. Returns {hit, hits, unknown_tags, "
            "no_coverage, searched_scope, fallback_reason}. Each hit "
            "carries {path, title, score, snippet} where snippet is "
            "the matched line's ~200-char window. Use the snippets to "
            "decide which paths to read next; do NOT call read for "
            "every hit."
        ),
    )(_search)
    agent.tool(
        name="read",
        description=(
            "Read verbatim bodies for the given paths. Returns "
            "{path: body}. Accepts library paths (<collection>/<page>) "
            "and wiki page IDs (page-<sha1-12>). Per-path failures are "
            "skipped + logged inside the tool; an all-fail result "
            "raises ToolError so the LLM can fall back to search "
            "snippets."
        ),
    )(_read)
