"""Librarian subagent — staged retrieval dispatch.

The LLM runs a 4-step pipeline: **Classify** (build a ``tag_expr`` from
the live registry), **Search** (one hybrid vec+lex qmd query returning a
``SearchResult`` envelope with snippets), **Read** (review the snippets,
then read only the paths that cover the question), **Return** (package
the excerpts into :class:`LibrarianOutput`).

Unlike ask's harness-``Agent``-tool dispatch, lies runs in-process
via ``librarian_agent.run_sync(deps)``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
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
        tag_expr: Compiled include AST (a TagExpr tree), or ``None`` for
            untagged queries. An AST rather than a string because the
            grammar accepts compound includes.
        exclude_expr: Compiled exclude AST, or ``None`` when no ``-``
            chain was supplied.
        top_k: Maximum number of excerpts to return (default 5).
    """

    question: str
    tag_expr: Any  # TagExpr | None AST; Any at runtime so pydantic-ai's
    # TypeAdapter doesn't try to build a schema for the AST (a stdlib
    # base class whose concrete variants are frozen dataclasses).
    exclude_expr: Any  # TagExpr | None AST, same reason.
    top_k: int = 5


@dataclass(frozen=True)
class PageExcerpt:
    """A page excerpt the librarian curated for the question.

    ``spans`` carries the structured span view rather than a raw text
    blob. ``source_kind`` flags which surface the excerpt came from, so
    rendering can distinguish primary-source hits from wiki-only hits;
    library-wins-on-conflict means there is no merged third value.
    """

    collection: str
    slug: str
    title: str
    spans: list[Span]
    source_kind: Literal["library", "wiki"] = "library"


@dataclass(frozen=True)
class LibrarianOutput:
    """The librarian's evidence bundle — verbatim passages for the synthesizer.

    The librarian does not write the answer; the synthesizer holds the cited
    synthesis. ``transient`` means the *dispatch* failed, so the bundle says
    nothing about the corpus, and it is set together with
    ``no_coverage=False``.

    ``fallback_reason`` is the search envelope's own string, mirrored. It is
    not taken on the model's word: :class:`SearchDiagnostics` records the
    same string independently and :func:`with_search_diagnostics` replaces
    whatever the model emits with it.
    """

    tag_expr: Any  # TagExpr | None AST, same reason as LibrarianDeps.tag_expr.
    exclude_expr: Any  # TagExpr | None AST, same reason.
    excerpts: list[PageExcerpt]
    distinct_pages: int
    no_coverage: bool = False
    searched_scope: list[str] = field(default_factory=list)
    transient: bool = False
    fallback_reason: str | None = None


@dataclass
class SearchDiagnostics:
    """The ``search`` tool's dispatch verdict, captured outside the model.

    One record per librarian dispatch, handed to
    :func:`register_librarian_tools` and read back after ``run_sync``
    returns. It is an argument rather than a module global so overlapping
    ``lib_ask`` calls cannot share one record.

    ``attested_paths`` is every page path a tool actually returned — each
    ``hit``/``hits[*].path`` and each ``read`` result key — and it is what
    :func:`with_search_diagnostics` checks an excerpt against. ``transient``
    is monotonic: a later clean envelope does not clear it.
    """

    transient: bool = False
    fallback_reason: str | None = None
    searched_scope: list[str] = field(default_factory=list)
    attested_paths: set[str] = field(default_factory=set)

    def record(self, envelope: dict[str, Any]) -> None:
        """Folds one ``search`` envelope into the record.

        A failure sticks: the reason a ``QmdLockBusy`` took to produce is
        the one the operator needs, even if a later envelope is clean.
        """
        if not isinstance(envelope, dict):
            return
        if envelope.get("transient"):
            self.transient = True
        reason = envelope.get("fallback_reason")
        if reason:
            self.fallback_reason = str(reason)
        scope = envelope.get("searched_scope")
        if scope:
            self.searched_scope = [str(name) for name in scope]
        for path in _envelope_hit_paths(envelope):
            self.attested_paths.add(path)

    def record_read(self, result: Any) -> None:
        """Folds one ``read`` result into :attr:`attested_paths`.

        Keys beginning with ``_`` are skipped: ``"_missing"`` is not a page.
        """
        if not isinstance(result, dict):
            return
        for key in result:
            if isinstance(key, str) and key and not key.startswith("_"):
                self.attested_paths.add(key)


_DISPATCH_RECORD: ContextVar[SearchDiagnostics | None] = ContextVar(
    "lies_librarian_search_diagnostics", default=None
)


def current_search_diagnostics() -> SearchDiagnostics | None:
    """The record bound to *this* dispatch, or ``None`` when none is.

    Long-lived tool closures read this instead of capturing a record, so
    an agent whose tools are registered once can still be attested
    per dispatch. Isolated by ``contextvars``: each thread and each
    asyncio task gets its own copy, so overlapping dispatches never see
    each other's record.
    """
    return _DISPATCH_RECORD.get()


@contextmanager
def search_diagnostics_scope(diag: SearchDiagnostics | None) -> Iterator[None]:
    """Bind ``diag`` as the current dispatch's record; restore on exit.

    ``None`` clears the binding, which is the "behave as today" arm for
    a caller that has nothing to attest.
    """
    token = _DISPATCH_RECORD.set(diag)
    try:
        yield
    finally:
        _DISPATCH_RECORD.reset(token)


def _envelope_hit_paths(envelope: dict[str, Any]) -> list[str]:
    """Paths a ``search`` envelope returned, in wire order.

    Reads both ``hit`` and ``hits``; a row may be a bare path or a dict.
    """
    paths: list[str] = []
    candidates: list[Any] = []
    top = envelope.get("hit")
    if top is not None:
        candidates.append(top)
    hits = envelope.get("hits")
    if isinstance(hits, list):
        candidates.extend(hits)
    for row in candidates:
        if isinstance(row, str):
            if row:
                paths.append(row)
        elif isinstance(row, dict):
            path = row.get("path")
            if isinstance(path, str) and path:
                paths.append(path)
    return paths


def _attestation_reason(dropped: list[PageExcerpt]) -> str:
    """Name the pages a bundle cited that no tool ever returned."""
    names = sorted({excerpt.slug for excerpt in dropped})
    listed = ", ".join(names[:3])
    if len(names) > 3:
        listed += f", +{len(names) - 3} more"
    return (
        f"{len(dropped)} of the librarian's excerpt(s) cited pages no tool returned "
        f"({listed}); they were dropped unverified"
    )


def with_search_diagnostics(out: LibrarianOutput, diag: SearchDiagnostics) -> LibrarianOutput:
    """Validates and overlays the recorded search verdict onto a bundle.

    Drops excerpts whose ``slug`` is in neither the recorded search hits nor
    the recorded ``read`` results, naming each drop in ``fallback_reason``.
    An all-attested bundle is returned untouched; a *total* drop forces
    ``transient=True`` and ``no_coverage=False``, so an invented bundle
    reads as a failed run rather than a claim about the corpus. A recorded
    ``fallback_reason`` replaces the model's copy verbatim.

    Every dispatch site is covered — see AGENTS.md, "The qmd access seam" for
    the three mechanisms. Attestation there is **path-level, not text-level**.
    """
    if out.excerpts:
        kept = [excerpt for excerpt in out.excerpts if excerpt.slug in diag.attested_paths]
        dropped = [excerpt for excerpt in out.excerpts if excerpt.slug not in diag.attested_paths]
        if not dropped:
            # Every excerpt is attested: the bundle is evidence, untouched.
            return out
        reason = _attestation_reason(dropped)
        if diag.fallback_reason:
            # The operator-actionable one is appended, never replaced.
            reason = f"{reason}; {diag.fallback_reason}"
        if not kept:
            # Forced, not merged: a wholly invented bundle is a failed run.
            return replace(
                out,
                excerpts=[],
                distinct_pages=0,
                transient=True,
                no_coverage=False,
                searched_scope=diag.searched_scope or out.searched_scope,
                fallback_reason=reason,
            )
        return replace(
            out,
            excerpts=kept,
            distinct_pages=len({excerpt.slug for excerpt in kept}),
            fallback_reason=reason,
        )
    if diag.fallback_reason or diag.transient:
        # Guarded on the verdict as well as its reason: a recorded
        # ``transient`` carrying no reason is still the recorded truth,
        # and dropping it would trade a fact for a string.
        transient = out.transient or diag.transient
        return replace(
            out,
            fallback_reason=diag.fallback_reason or out.fallback_reason,
            transient=transient,
            no_coverage=False if diag.transient else out.no_coverage,
            searched_scope=diag.searched_scope or out.searched_scope,
        )
    return out


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
`{hit, hits, unknown_tags, no_coverage, transient, searched_scope,
fallback_reason}`.

`hits` is a list of `{path, title, score, snippet}` rows; each
snippet is a ~200-char window around the matched line in the source
markdown.

- `unknown_tags` non-empty → return an empty bundle with the
  unknown spec in the envelope. Do NOT silently retry without tags.
- `no_coverage=True` → return an empty bundle; the corpus has zero
  hits for this question.
- `transient=True` → the search **did not finish** (qmd outlived its
  deadline, or the site-wide qmd lock was held past your budget). This
  is a fact about the run, not about the corpus: the search learned
  nothing, so it has no standing to say the library lacks this topic.
  Retry the same search **once**. If it is still transient, return an
  empty bundle with `no_coverage=False` and `transient=True`, copy
  the envelope's `fallback_reason` onto the bundle **verbatim**, and
  say the lookup was inconclusive — never that the content is missing.
- `fallback_reason` non-None with `transient=False` → **not** a daemon
  failure. This arm was previously described as "the daemon genuinely
  failed", which is wrong: `search` sets `transient=False` alongside a
  reason for an *empty question*, an unknown tag, a tag expression that
  resolved to no collections, no collections registered, or a collection
  that is registered but has no documents. Four of those are facts about
  the request and one about the index; the daemon is answering fine in
  every case. Return an empty bundle, copy the `fallback_reason` onto
  the bundle verbatim, and say the lookup was inconclusive — the same
  "ask again" posture as the transient arm above, because the reason
  string tells the reader which of those it was.

`no_coverage` and `transient` are deliberately separate flags. An
earlier version set `no_coverage=True` on a timeout, and because this
contract told the model that flag means "the corpus has zero hits",
a slow call reached the user as "No relevant content found in
library." — a false statement about the corpus, for a question that
answered in under six seconds on the retry.

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

  Every `slug` must be a path a tool returned to you — a `hits[*].path`
  from Step 2 or a key of a `read` result from Step 3. The caller
  records every one of those independently and **drops** an excerpt whose
  path is in neither set, naming the drop in the reason. That is a
  determinism check, not a style rule: an excerpt for a page nothing
  returned is indistinguishable, to every downstream reader, from a real
  citation.

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
- `fallback_reason` — mirror the search tool's `fallback_reason`
  verbatim, or `null` when the search did not report a dispatch
  failure. Copy the string; do not summarise it, do not shorten it,
  and do not invent one. **This is good hygiene, not the delivery
  mechanism**: the caller records the search envelope's verdict
  independently and overwrites this field with the verbatim string
  when the search reported a failure, so a paraphrase here cannot
  reach the user either way. Still copy it — a search that queued
  behind the site-wide qmd lock and gave up puts the holder PID, the
  elapsed wait, the budget and the contended inode in exactly this
  field, and the synthesizer reads your bundle before the caller
  corrects it. An empty bundle that names nothing reads as "the
  corpus has nothing", which is the false claim this whole arm
  exists to prevent.

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
  "searched_scope": [...],
  "fallback_reason": null
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
  "searched_scope": ["opencode"],
  "fallback_reason": null
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
    """Invokes a FastMCP tool, preferring its ``.fn`` over a direct call.

    Tests patch the module attribute with a lambda or ``MagicMock`` that
    has no ``.fn``; both shapes reach the same closure.
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
    search_diagnostics: SearchDiagnostics | None = None,
) -> None:
    """Registers ``collections_read`` / ``search`` / ``read`` on the librarian agent.

    Each closure is a thin adapter to the corresponding MCP tool. Call once
    per agent instance after construction; the pydantic-ai decorator raises
    on double-register with the same name. ``wiki``, ``memory_service``,
    ``qmd_query`` and ``qmd_get`` are accepted-but-ignored, kept for the
    orchestrator's and :func:`lies.mcp.grounding.ground`'s call sites.

    ``search_diagnostics`` is the optional per-dispatch record of the
    ``search`` envelope. When ``None`` the closures fall back to the record
    bound by :func:`search_diagnostics_scope`, and to no recording when
    neither is set — it is per *dispatch*, not per agent.
    """
    del wiki, memory_service, qmd_query, qmd_get  # back-compat: v0.39 callers

    def _record() -> SearchDiagnostics | None:
        """The record this tool call folds into: explicit, else ambient."""
        return (
            search_diagnostics if search_diagnostics is not None else current_search_diagnostics()
        )

    def _collections_read(
        ctx: RunContext[LibrarianDeps],
        subcommand: Literal["list", "tag_list", "info"],
        name: str | None = None,
    ) -> list[dict[str, Any]] | dict[str, Any]:
        """Reads the live library registry.

        ``list`` returns a row per collection, ``tag_list`` a
        ``tag → collections`` map, ``info`` + ``name=…`` one
        collection's full metadata.
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
        """Runs a single-batch hybrid vec+lex qmd query.

        Returns a :class:`SearchResult` envelope whose ``hits`` rows
        carry a ``~200``-char snippet for the model's Step 3 review.
        The envelope is recorded before the model sees it.
        """
        from lies.mcp import search as _search_mod

        envelope = _call_tool(
            _search_mod.search,
            question=question,
            tag_expr=tag_expr,
            exclude_tags=exclude_tags,
            hypothetical=hypothetical,
        )
        record = _record()
        if record is not None:
            record.record(envelope)
        return envelope

    def _read(
        ctx: RunContext[LibrarianDeps],
        paths: list[str],
    ) -> dict[str, str]:
        """Reads verbatim bodies for the given paths.

        Returns ``{path: body}``, skipping per-path failures. The keys
        fold into the same :class:`SearchDiagnostics` the ``search``
        closure feeds, after the call returns — ``read`` raises
        ``ToolError`` when every path fails.
        """
        from lies.mcp import read as _read_mod

        result = _call_tool(_read_mod.read, paths=paths)
        record = _record()
        if record is not None:
            record.record_read(result)
        return result

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
