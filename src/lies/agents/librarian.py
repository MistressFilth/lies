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
        tag_expr: Compiled include AST (Task 3 / f15-exclude-compound),
            a TagExpr tree. ``None`` for untagged queries. Carries
            the AST rather than a flat string because the F15 grammar
            accepts compound includes (``+c:foo&c:bar``,
            ``+c:foo|c:bar``) whose per-collection dispatch walks
            the tree. Historical flat-string contract retired in
            Task 3 along with the ``ResolvedTagFilter.exclude``
            counterpart.
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
    tag_expr: Any  # TagExpr | None AST; Any at runtime so pydantic-ai's
    # TypeAdapter doesn't try to build a schema for the AST (a stdlib
    # base class whose concrete variants are frozen dataclasses, not
    # pydantic models).
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
        transient: v0.47 additive — the *dispatch* failed, so the
            bundle says nothing about the corpus. A model outage, a
            ``UsageLimitExceeded``, a response pydantic-ai could not
            validate after its retry budget: each is a fact about
            the run, and this branch existed with only
            ``no_coverage=True`` to report it with, which is the
            canonical false corpus claim. Set together with
            ``no_coverage=False``; the two are independent, exactly
            as on :class:`~lies.mcp.grounding.ArchivistDigest` and
            the ``search`` envelope. Defaults to ``False``.
        fallback_reason: v0.48 additive — the search envelope's own
            ``fallback_reason``, mirrored verbatim, when Step 2's
            ``search`` reported a dispatch failure. The bundle is the
            boundary the librarian's evidence crosses, and until this
            field existed the envelope's reason died there: ``search``
            already names the qmd lock holder, the wait and the budget,
            and ``lib_ask`` replaced all of it with one fixed sentence.

            **The model's copy is a claim, not the delivery path.**
            :class:`SearchDiagnostics` records the same string
            independently and :func:`with_search_diagnostics` replaces
            whatever the model emits with it, so this field is hygiene
            for the bundle the synthesizer reads rather than the only
            channel to the reader. ``None`` whenever the search
            succeeded or the failure was not a named dispatch failure.
    """

    tag_expr: Any  # TagExpr | None AST; Any at runtime so pydantic-ai's
    # TypeAdapter doesn't try to build a schema for the AST (a stdlib
    # base class whose concrete variants are frozen dataclasses, not
    # pydantic models).
    exclude_expr: Any  # TagExpr | None AST (Task 3); Any at runtime so
    # pydantic-ai's TypeAdapter doesn't try to build a schema for
    # TagExpr (a stdlib @dataclass, not pydantic).
    excerpts: list[PageExcerpt]
    distinct_pages: int
    no_coverage: bool = False
    searched_scope: list[str] = field(default_factory=list)
    transient: bool = False
    fallback_reason: str | None = None


@dataclass
class SearchDiagnostics:
    """The ``search`` tool's dispatch verdict, captured outside the model.

    **Why this exists.** ``_search_impl`` catches ``QmdLockBusy`` and
    returns an envelope — it never raises — so the string naming the
    lock holder, the elapsed wait, the budget and the contended inode
    has to travel to the user through the librarian LLM. Every other hop
    is code; that one was a prompt instruction, and a model that
    paraphrases, summarises or drops the field silently converts "you
    queued behind PID 4242 for 120 s" into "dispatch failed". The
    instruction is still in the contract (it keeps the field honest in
    the transcript and in front of the synthesizer), but it is no
    longer load-bearing.

    One record per librarian dispatch, handed to
    :func:`register_librarian_tools` and read back by the caller after
    ``run_sync`` returns. It is an argument rather than a module global
    on purpose: the MCP server serves concurrent ``lib_ask`` calls, and
    a global would let one call's lock contention land on another's
    envelope.

    Attributes:
        transient: The last ``search`` envelope reported ``transient=True``
            — the dispatch did not finish. Monotonic: once a search has
            failed this way it stays set, because a bundle assembled
            after a failed search cannot claim to have learned nothing
            about the corpus.
        fallback_reason: The most recent non-empty ``fallback_reason``
            the tool produced, verbatim. Recorded for *every* reason,
            transient or not: the contract has an arm for a non-transient
            reason too (an unknown tag, a registered-but-empty
            collection) and those name the same kind of fact.
        searched_scope: The resolved collection list from the last
            envelope that carried one.
        attested_paths: Every page path a tool actually handed to the
            model — each ``hit``/``hits[*].path`` from a ``search``
            envelope, and every key of a ``read`` result. This is the
            oracle :func:`with_search_diagnostics` validates a bundle's
            excerpts against. It is *not* the search envelope alone; see
            that function for why the envelope on its own is the wrong
            reference.
    """

    transient: bool = False
    fallback_reason: str | None = None
    searched_scope: list[str] = field(default_factory=list)
    attested_paths: set[str] = field(default_factory=set)

    def record(self, envelope: dict[str, Any]) -> None:
        """Fold one ``search`` envelope into the record.

        A failure sticks; a later clean envelope does not clear it. The
        librarian contract tells the model to retry a transient search
        once, so the record deliberately keeps the first failure rather
        than the last verdict — a run that failed twice and then
        succeeded still spent its budget queueing, and the reason that
        took a real ``QmdLockBusy`` to produce is the one the operator
        needs. ``with_search_diagnostics`` is where that decision is
        bounded (an evidence-bearing bundle is never overwritten).

        The envelope's hit paths fold into :attr:`attested_paths` on the
        way through. Recording the *paths* as well as the verdict is what
        makes an excerpt checkable: a bundle names a ``slug``, and this is
        the recorded answer to "did any tool ever return that path".
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
        """Fold one ``read`` result into :attr:`attested_paths`.

        ``read`` returns ``{path: body}`` plus a synthetic ``"_missing"``
        key holding the paths the daemon could not resolve
        (``mcp/read.py``). Keys beginning with ``_`` are dropped:
        ``"_missing"`` is not a page, and attesting it would let a bundle
        claim a document for the one path that was explicitly reported as
        unresolvable.

        Recorded *beside* the search hits rather than instead of them,
        because an excerpt's path is not by construction a search hit.
        The contract says the model picks hits to read, and it may read a
        path a body's link sent it to — ``read`` accepts any path the
        library or the wiki resolves. A bundle built from such a read is
        evidence, and dropping it would discard retrievable evidence to
        enforce a rule the tools never imposed.
        """
        if not isinstance(result, dict):
            return
        for key in result:
            if isinstance(key, str) and key and not key.startswith("_"):
                self.attested_paths.add(key)


def _envelope_hit_paths(envelope: dict[str, Any]) -> list[str]:
    """Paths a ``search`` envelope actually returned, in wire order.

    Reads both ``hit`` (the top-1 row) and ``hits``. They are normally
    the same document, but the top-1 field is set independently by
    ``_search_impl`` and a bundle built from the top-1 snippet alone is
    legitimate — the contract's "fall back to the snippets already
    attached to the search hits" arm reads either.

    A hit may be a bare path string or a ``{path, ...}`` row depending on
    which layer produced it; both are accepted, and anything else is
    skipped rather than coerced, because coercing ``None`` to ``"None"``
    would attest a page that does not exist.
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
    """Validate and overlay the recorded search verdict onto a bundle.

    This is the deterministic last hop. Before it existed the only route
    from the ``search`` envelope's ``fallback_reason`` to the reader was
    the model copying the string onto its own output, so a
    non-compliant model degraded a lock-busy report to a generic reason
    with no corpus claim.

    Four rules, each of which the model cannot talk its way past:

    - **Every excerpt must be attested.** An excerpt whose ``slug`` is in
      neither the recorded search hits nor the recorded ``read`` results
      is dropped, and the drop is named in ``fallback_reason``. This is
      the rule the branch was previously *documenting rather than
      enforcing*: a model asserting excerpts it does not have used to get
      its bundle back untouched, and ``_ask_impl`` then rendered prose
      citing pages nobody read. See "Why hits alone are not the oracle"
      below for what the reference set is and why.
    - **Evidence wins.** A bundle whose excerpts are *all* attested is
      returned unchanged. It was assembled from real ``read`` bodies after
      a later, successful search; forcing ``transient`` onto it would make
      the caller throw away retrievable evidence because an earlier
      attempt queued.
    - **A recorded failure overwrites the bundle's own verdict.** When
      the search tool reported ``transient=True`` and the bundle claims
      otherwise, the tool is right and the model is not: nothing was
      dispatched, so the bundle cannot have learned what the corpus
      contains. That is why ``no_coverage`` is forced ``False`` — a
      failed dispatch is never a statement about the corpus.
    - **The reason is the tool's, verbatim.** The model's copy is
      replaced rather than merged. A paraphrase that keeps the word
      "lock" while dropping the pid, the wait, the budget or the lock
      path is precisely the defect this closes.

    A non-transient recorded reason (unknown tag, registered-but-empty
    collection) replaces the reason but leaves ``transient`` and
    ``no_coverage`` alone: those describe a dispatch that *ran*, and
    overwriting them would convert a corpus statement into a run
    statement.

    **What an all-dropped bundle becomes.** Every excerpt gone means the
    bundle carries no evidence at all, so it is a fact about the run and
    not about the corpus: ``transient`` is forced ``True`` (not merged —
    a successful search beside an invented bundle is still a failed
    lookup), ``no_coverage`` is forced ``False``, and ``_ask_impl`` takes
    its inconclusive branch. A partly-dropped bundle keeps the excerpts
    that were attested — evidence still wins — and carries the drop in
    its reason, because an operator whose librarian invented two of five
    citations is entitled to be told.

    **Why hits alone are not the oracle.** The ruling this implements
    proposed dropping any excerpt missing from the envelope's ``hits``.
    That is the wrong reference set, and the library is why: step 3 says
    the model *picks* from hits, but ``read`` accepts any path the
    library or a wiki resolves, and a model that followed a link found in
    a body and read that page has produced evidence the envelope never
    named. Attesting against hits alone would drop it — silently
    discarding retrievable evidence to enforce a rule the tool surface
    never imposed, which is the exact trade the overlay was built to stop
    making. Recording the ``read`` results alongside the hits makes the
    oracle "a tool returned this path", which is the claim the excerpt is
    actually making, and it is checkable with no false positives on any
    path the tools did return.

    **What it still does not check, and what closing it would need.** A
    model that reads a real page and then attributes *invented text* to
    it passes: only the path is verified. Closing that needs the bodies,
    not the paths — ``SearchDiagnostics`` would have to retain
    ``path -> body`` from ``record_read`` (and the hit snippets, for the
    snippet-only arm, which is the one a model reaches by choosing not
    to read rather than by surviving a ``ToolError``) and each
    ``Span.body`` would be checked as a substring of the recorded text.
    That is a real trade rather than a small addition: a model that
    normalises whitespace, reflows a paragraph or joins two spans
    produces a body that is not a substring of the page, and every such
    excerpt would be dropped as fabricated. The false-positive rate is
    not measurable from here, and discarding true evidence is the worse
    failure of the two. So the text-level check is not shipped, and
    saying so is more honest than shipping a substring test that quietly
    deletes good citations.

    Returns ``out`` itself when there is nothing to overlay, so a clean
    run allocates nothing.
    """
    if out.excerpts:
        kept = [excerpt for excerpt in out.excerpts if excerpt.slug in diag.attested_paths]
        dropped = [excerpt for excerpt in out.excerpts if excerpt.slug not in diag.attested_paths]
        if not dropped:
            # Evidence wins, unchanged: same object, same fields.
            return out
        reason = _attestation_reason(dropped)
        if diag.fallback_reason:
            # The operator-actionable one is appended, never replaced.
            reason = f"{reason}; {diag.fallback_reason}"
        if not kept:
            # ``transient`` is forced, not merged: a bundle whose every
            # excerpt was fabricated is a failure of the *run* whatever
            # the model or the search envelope claimed about it. Merging
            # (``out.transient or diag.transient``) left a successful
            # search plus an invented bundle reading as
            # ``transient=False``, and ``_ask_impl``'s next branch —
            # ``if not lib_out.excerpts`` — then rendered "No relevant
            # content found in library." That is the canonical false
            # corpus claim, manufactured by the fix for it.
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
    if diag.fallback_reason:
        transient = out.transient or diag.transient
        return replace(
            out,
            fallback_reason=diag.fallback_reason,
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
    search_diagnostics: SearchDiagnostics | None = None,
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

    ``search_diagnostics`` is optional and defaults to ``None``, which
    is the pre-existing behaviour exactly: the ``_search`` closure
    records nothing and the model's own bundle is the only account of
    what the search did. Supply one record per *dispatch* (not per
    agent) when the caller assembles the bundle itself and needs the
    search envelope's verdict to survive the model — see
    :class:`SearchDiagnostics`.

    **An argument, not a module global, and not a ``ContextVar`` —
    do not "fix" this into one.** Only ``synth.librarian_agent_run``
    reads the record, and it constructs one per call; the MCP server
    serves overlapping ``lib_ask`` requests, so a shared mutable
    module global would let one request's lock contention land on a
    different request's envelope. That is the argument.

    The other two registration sites pass ``None`` and are **not** at
    risk from any of this, which is worth writing down so nobody
    threads a record through them on the strength of a cross-talk
    story that does not apply to them:

    - ``mcp/grounding.py`` builds its digest from
      ``out.excerpts`` and ``out.no_coverage`` alone. It never reads
      ``transient`` or ``fallback_reason`` off a librarian bundle; its
      own lock diagnostics come from the two ``except QmdLockBusy``
      arms around its direct ``access.daemon_tool`` calls.
    - ``orchestrator.py`` registers the tools at ``__init__`` time on
      a long-lived agent, and ``run_query`` recomputes
      ``answer.no_coverage`` from the resolved tag scope rather than
      reading the bundle's.

    A ``ContextVar`` would change nothing for either: it would scope
    the record per task, which solves nothing they have, and it would
    make the one site that *does* need per-dispatch isolation look
    like it was getting a concurrency guarantee it does not need —
    the record is already constructed fresh on every call.

    Args:
        agent: The librarian agent whose toolset to populate.
        wiki: Accepted for back-compat with the F18 call site; unused.
        memory_service: Accepted for back-compat; unused.
        qmd_query: Accepted for back-compat; unused.
        qmd_get: Accepted for back-compat; unused.
        search_diagnostics: Optional per-dispatch record of the
            ``search`` tool's envelope. ``None`` disables recording.
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

        The envelope is handed to a caller-supplied
        :class:`SearchDiagnostics` on its way out, *before* the model
        ever sees it. That is the whole point of this closure recording:
        the tool's verdict is a fact about the dispatch, and the model
        is not the channel it should have to travel through. Recording
        here rather than in ``_ask_impl`` also means a caller that never
        reads the record pays nothing — the write is unconditional and
        the interpretation is the caller's.
        """
        from lies.mcp import search as _search_mod

        envelope = _call_tool(
            _search_mod.search,
            question=question,
            tag_expr=tag_expr,
            exclude_tags=exclude_tags,
            hypothetical=hypothetical,
        )
        if search_diagnostics is not None:
            search_diagnostics.record(envelope)
        return envelope

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

        The returned keys are folded into the same
        :class:`SearchDiagnostics` the ``search`` closure feeds, so a
        bundle is checked against the paths a tool actually returned
        rather than against what the model says it read. Recorded after
        the call returns, not before: ``read`` raises ``ToolError`` when
        every path fails, and a dispatch that raised returned nothing.
        """
        from lies.mcp import read as _read_mod

        result = _call_tool(_read_mod.read, paths=paths)
        if search_diagnostics is not None:
            search_diagnostics.record_read(result)
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
