# AGENTS.md

Source of truth for LLM agents working in the `lies` repository.

## Local-only Memory Files

@AGENTS.local.md

## Pre-PR checklist

Before opening or merging a PR, the agent MUST:

1. **Keep versioning bumps adherent to SemVer.** Bump the appropriate segment
   for the nature of the change. Update every version surface — see
   `~/.claude/rules/versioning.md`. In this repo those are `pyproject.toml`
   and `src/lies/__init__.py`.

   **One documented exception: the MCP prompt surface is host
   configuration, so a breaking change to it takes a minor bump.**
   `conventional-commits.md` says breaking is major, and for a library
   imported as a package that is right. The seven `@mcp.prompt`
   registrations are configuration a host reads to render a slash menu
   and a `get_prompt` tool schema; the tools (`collections_read`,
   `search`, `read`, `lib_ask`, `lint`, `reindex`) are the programmatic
   API and keep breaking-change major bumps. A caller that imported
   `lies` as a library never touches the prompt signatures, and a
   consumer pinning `lies>=0.42` gets the new prompt shape with the
   same install. Bump minor for a prompt-signature break; bump major
   for a tool-signature break. This decision is recorded here rather
   than only in `CHANGELOG.md` so the next agent to bump a version sees
   it before choosing a segment.

   **The `!` marker does not follow the segment.** Conventional Commits
   ties `!` to a major bump, and version-bump automation reads the
   marker. When the only break is on the prompt surface, write
   `feat(mcp)!:` anyway — the marker records that a caller-visible
   signature changed, which is true, and the version segment below
   records how seriously the project takes it. A bare `feat(mcp):`
   would hide the break from every Conventional-Commits-driven tool,
   including `make release`. If a change breaks prompts *and* tools,
   it is major and the marker means what it normally means.
2. **Keep `CHANGELOG.md` up to date.** Add an entry under the in-progress or
   new release section describing the change.
3. **Keep `README.md` up to date.** New commands, new config options, new
   install steps, behavior changes — all reflected in the README.
4. **Keep `docs/` up to date.** If the change is reflected there as well,
   keep it in sync.

## Pre-existing issues

Treat any "pre-existing" issue — one already on `main`, in the issue tracker,
or referenced in TODO/FIXME/XXX — **as if it is your own issue to solve**. Do
not dismiss as out-of-scope, historical, or someone else's problem. The first
encounter is yours; resolve or escalate.

## Project context

**LIES** — Library of Inconsistent Explanations & Sources. A
Karpathy-pattern LLM wiki: a `pydantic-ai`-harness agent maintains a
git-backed wiki of interlinked markdown files over a corpus of raw
sources. The schema (a per-wiki markdown file) defines page types,
conventions, and workflows. The human curates sources and asks
questions; the agent does all bookkeeping.

## Page-type conventions

Per-type required `## <Heading>` sections are declared in
`src/lies/schema/default_schema.md` under "Section contract" and
parsed at wiki-open time. Lint surfaces
`missing_required_section` findings (`safe_to_fix=False`); the
writer (`lies page write` CLI + MCP `file_knowledge`) refuses
writes that omit required sections. Override per-wiki via
`<wiki>/schema.md`. Match is literal-substring — `## Evidence`
matches, but `### Evidence`, `##Evidence`, and `## evidence` do
not. Full design at
`~/code/project-notes/lies/superpowers/specs/2026-09-18-f17-page-type-conventions-design.md`.

## Source layout

```
src/lies/
├── agents/          # sub-agent prompt YAMLs (source-reader, page-writer,
│                    # linter, librarian, query-synthesizer). No indexer — the catalog
│                    # is deterministic (see below).
├── capabilities/    # harness capability adapters (CodeMode, Memory, Planning, ...)
│   └── memory.py    # Harness Memory capability; per-wiki namespace via WikiIdentity
├── cli/             # Typer CLI package (init / ingest / query / lint / mcp / REPL)
│   ├── catalog.py   # `lies catalog` group: status/dump/reconcile/rebuild/render
│   └── qmd.py       # `lies qmd` operator group: status/up/down/recycle
│                    # (thin wrapper around lies.qmd.lifecycle)
├── config.py        # env-driven config (model, wiki root, log level)
├── library/         # global corpus of collection configs and deterministic
│   │                # mirrors. Per-cutover the library is the source of
│   │                # truth for collection metadata; wikis no longer own
│   │                # per-collection YAMLs.
│   ├── record.py                # LibraryCollectionConfig dataclass
│   ├── schema.py                # ConfigYAML Pydantic schema
│   ├── config_io.py             # atomic load_config / save_config helpers
│   ├── bootstrap.py             # idempotent bootstrap_library_collection
│   ├── collections_cli.py       # `lies library` sub-app
│   │                            # (list/show/where/new/modify/delete/enrich-tags)
│   └── migrate_collection_configs.py  # `lies migrate-collection-configs`
├── mcp/             # FastMCP server (src/lies/mcp/server.py) — library-mode read surface;
│                    # tools: collections_read, search, read, lib_ask, lint, reindex,
│                    # plus list_prompts + get_prompt generated by the PromptsAsTools
│                    # transform; prompts: ask, ground, collections, ingest, lint,
│                    # reindex, sync; resources: library://catalog and
│                    # library://catalog/{slug} (wiki://status, wiki://index,
│                    # wiki://log, wiki://lint-report kept as operational
│                    # diagnostics); the wiki-shaped read tools (query / answer /
│                    # wiki_search / wiki_read / wiki_changes / file_knowledge)
│                    # and wiki-shaped data resources (wiki://page /
│                    # wiki://memory-changes / wiki://catalog) are retired
│   ├── prompts.py   # register_prompts(mcp) entry point for the 7 slash prompts
│   ├── prompts_impl.py  # per-prompt impl functions + _split_tail / _parse_question_filters
│   │                # + register_all decorator wiring
│   ├── grounding.py # F19 grounding archivist (CitationSnippet + ArchivistDigest
│   │                # + truncate_at_word_boundary + pick_first_prose_span + ground())
│   ├── synth.py     # library-mode synthesize envelope (SynthesizeEnvelope + synthesize())
│   └── daemon.py    # pidfile lifecycle for `lies mcp up/down/status`
├── memory/          # invisible-memory layer (see below)
│   ├── catalog.py   # sqlite wiki catalog: schema + CRUD + rebuild_from_disk
│   │                # + reconcile + render_markdown. Mirrors the design
│   │                # described in superpowers/specs/2026-09-04-f4b-f16-catalog-port-design.md
│   │                # Lives at <wiki_dir>/.lies/catalog.db
│   └── catalog_models.py  # CatalogPage (frozen BaseModel) + PageSection enum
├── orchestrator.py  # top-level Orchestrator; owns cross-cutting capabilities
├── qmd/             # qmd CLI + MCP adapters
│   ├── _models.py   # Pydantic models returned by qmd library functions (e.g. ReindexResult)
│   ├── _proc.py     # subprocess seam for qmd library functions (Popen + bounded communicate)
│   ├── _subprocess.py # deadlock-free `_run_qmd` helper (Popen + timeout + SIGKILL-on-overrun)
│   ├── capability.py # daemon-aware QmdCapability (MCP toolset + recycle envelope)
│   ├── cli.py       # thin wrapper around the `qmd` CLI for batch operations
│   ├── daemon.py    # ensure/inspect qmd's own daemon (never stops it)
│   ├── health.py    # cheap reachability probe for the qmd HTTP daemon
│   ├── lifecycle.py # qmd daemon lifecycle: status, up, down, recycle
│   ├── lock.py      # cross-process flock envelope for qmd CLI helpers
│   ├── mcp.py       # qmd MCP client (QmdRecycleToolset wrapper for transport errors)
│   └── mcp_fallback.py # in-process FastMCP fallback for the qmd HTTP daemon
├── markdown_spans.py # F37 — markdown spans parser (Span dataclass + parse_spans)
├── query/           # index.md parser + answer synthesizer
│   ├── citation.py  # Citation / ClaimCitation dataclasses (F19)
│   └── ...
├── schema/          # default schema + loader
├── utils/           # logging, shell helpers, exclusive.py (create-lock +
│                    # gitignore guard shared by heartbeat and mcp daemon)
└── wiki/            # git + layout primitives
    ├── registry.py          # WikiCollectionRef registry (per-wiki, in-memory)
    └── registry_errors.py   # typed errors for registry lookups
```

Tests mirror the layout:

```
tests/
├── unit/            # unit tests
├── integration/     # end-to-end tests (this project uses tests/integration/;
│                    # tests/features/ is also accepted by the Makefile)
├── mcp/             # FastMCP server tests
├── fixtures/        # shared fixture assets
└── conftest.py
```

## Runtime state (project memory)

The project runtime on the host currently operates
**knowledge-collections-only** — no wikis are registered under
`~/.local/share/lies/<name>/`. The MCP `synthesize` / `cite` /
`ground` tools fan out across the global library at
`~/.local/share/lies/library/collections/<name>/` instead.

When an agent is asked to "look at a lies collection," treat it as a
reference to a library collection, not a wiki. The library is the
source of truth for retrieval in this environment.

Wiki code paths (`WikiMemoryService`, `init_wiki`, `WikiIdentity`,
`MemoryPlan`, page-author agents) remain in source for future use.
They are dormant — no wiki XDG instance currently exists for them
to point at. `lies init <name>` will create a new wiki if invoked;
that's expected for future wiki-mode users.

### Read-side surface (post #103/#104/#106, 2026-09-25)

The library-mode read surface is split between two MCP tools:

- **`ground`** — snippet digest for agents. `ArchivistDigest` with
  `[[collection/slug]] (Title): "<verbatim snippet>"` rendering.
  Uses sequential qmd fan-out (`_fanout_collections._one` awaited
  one at a time in a `for` loop) — one qmd subprocess at a time,
  full stop. Each concurrent `qmd_query` independently loads the
  embedding model into VRAM, so OR-scoped queries
  (`+c:opencode|c:claude_code`) used to spike VRAM when two
  subprocesses fired at once. Per-call timeout lives in
  `LIES_QMD_FANOUT_TIMEOUT` (default 15s; matches qmd's observed
  reranking latency on cold daemons). The recycle trigger counts
  only `QmdCommandError` (real subprocess failures);
  `QmdNoResultsError` (clean miss) is silent.
- **`synthesize`** — prose answer for humans. `SynthesizeEnvelope`
  carrying the LLM-written body and claim-tagged citations. Calls
  `await ground(...)` (no longer shelled through `asyncio.run`),
  then `await query_synthesizer_agent.run(...)`. Empty digest
  surfaces honest gap prose (`"No relevant content found in library."`)
  with `synthesis_used=False`. Returns `SynthesizeEnvelope(answer="")`
  on `ModelNotConfigured` with `fallback_reason` carrying the
  exception class + message.

The MCP `ground` tool wrapper (`mcp_ground` in `server.py`) is sync
and bridges to the async `ground()` via `asyncio.run(...)`. Works
today because FastMCP runs sync handlers in a threadpool; migration
to native async support deferred.

- `lies sync` chains qmd `update` + `embed` after the collection
  loop. Use `--skip-reindex` to opt out for CI matrices.

### Library mode in tests (post #104)

`tests/conftest.py::_isolated_xdg` autouse fixture seeds a
deterministic `providers.toml` under `<tmp_path>/config/lies/` with
`[providers.minimax]` + `[providers.anthropic]` + `[agents]` (including
`librarian`) + `MINIMAX_API_KEY=test-key-not-real`. Tests that exercise
the MCP path no longer hit `ModelNotConfigured` against the bundled
`agents` roster. Library collection registry is empty in unit-test
mode; the test fixture for `mcp_ground` constructs the digest from
mocked librarian excerpts (see `tests/unit/mcp/test_ground.py`).

## Invisible memory layer

`src/lies/memory/` is the invisible-memory layer:

- `WikiMemoryService` (in `service.py`) is the **single owner** of wiki
  mutation for memory: it validates plans, applies operations, snapshots
  the working tree, commits atomically, restores on failure, and refreshes
  the qmd derived index.
- `catalog.py` is the sqlite wiki catalog at `<wiki_dir>/.lies/catalog.db`
  (WAL, `busy_timeout=5000`) — the catalog source-of-truth, replacing
  `wiki/index.md`. `apply_plan` upserts one row per operation inside the
  flock + atomic-commit envelope; the etl WRITE stage bulk-upserts every
  written path. Both paths are deterministic: **no model call belongs
  inside the lock**, which is what F4b's design turned on. `wiki/index.md`
  is a read-only title-only derivative rendered by `lies catalog render`.
  Drift from out-of-band edits is fixed explicitly with
  `lies catalog reconcile`, never implicitly.
- `capabilities/memory.py` exposes this through the harness `Memory`
  capability with a per-wiki namespace derived from `WikiIdentity` (so
  two wikis against the same install do not share state).
- The FastMCP server exposes the library-mode read surface
  (`lib_ask` for prose answers, `search` for snippet digests, `read`
  for verbatim bodies) against the global library at
  `~/.local/share/lies/library/collections/<name>/`. The Pydantic
  AI main agent and any future wiki-mode user reach memory through
  the dormant wiki-shaped tools (no production surface).
- **Routing user questions through the prompt surface, not `lib_ask`
  directly.** `lib_ask` is the synthesizer that runs *inside* the
  `ask` prompt's body, not a user entry point. An agent answering a
  user's question should call `get_prompt(name="ask" | "ground",
  arguments={"tail": "<full user message>"})`; the prompt body
  parses `+tag` / `-tag` filter tokens out of the tail and renders
  the routed `search` / `read` / `lib_ask` calls with the typed filter.
  Calling `lib_ask` directly skips that parsing and the
  citation-render instructions. Hosts that bind slashes to MCP prompts
  pre-tokenize the slash tail on whitespace and bind tokens
  positionally, so the slash path is **single-token only** — everything
  after the first token is overflow and is dropped. `get_prompt` is the
  path for any multi-word question.
- **Prompt tails take one `str` and parse their own flags.** Every
  prompt declares exactly one string parameter, named `tail` for all
  seven; a typed `bool` / `int` / `list[str]` past position one receives
  a bare word from the slash tokenizer and fails JSON decode. One slot
  makes the slash path *safe*, not *complete* — the host binds exactly
  one token and drops the rest, so a multi-token tail reaches a prompt
  whole only through `get_prompt`.

  `ask` was briefly left as `question`, and the name is load-bearing
  rather than cosmetic. FastMCP filters `get_prompt` arguments down to
  the declared signature, so a surviving name means a *retired* call
  succeeds instead of raising: `{"question": …, "tag_expr": …}`
  rendered a well-formed body that searched with `tag_expr=None` and
  silently dropped the filter — a query the user scoped by tag,
  answered from the whole library, with nothing in the response saying
  so. Renaming it to `tail` makes `ask` fail as loudly as the other
  six (`Missing required arguments: {'tail'}`). A prompt parameter
  that survives a signature change is a silent break, not a
  compatible one.
  `prompts_impl._split_tail` is the shared parser: it splits on
  whitespace only (never a shell lexer — an apostrophe in ordinary
  English must not raise), separates flag values from bare flags, and
  reports value flags that got no value, a value attached to a boolean,
  and flags outside a prompt's vocabulary so the body can name the
  problem instead of running a command the user did not ask for. Every
  prompt declares its own `value_flags` / `multi_word_flags` /
  `known_flags`; a body advertising a flag the parser does not know is
  a defect, and `tests/unit/mcp/test_split_tail.py` (parser) plus
  `tests/unit/mcp/test_prompts_flag_vocabulary.py` (each prompt's
  advertised flags surviving the parse into the rendered command) cover
  the two halves.
- **A flag vocabulary is transcribed from the target command's Typer
  signature, and checked against it.** A body that renders a
  `Bash(...)` line is promising the agent a runnable command, so every
  flag it emits must be an option the command declares and the
  positional count must fit its arguments. `prompts_impl`'s
  `_LIBRARY_VERB_FLAGS` / `_INGEST_*` / `_SYNC_*` tables are that
  transcription, and
  `tests/unit/mcp/test_rendered_commands_are_runnable.py` resolves
  every rendered command against the live Typer app. The vocabulary
  test cannot catch this class on its own: it proves a flag reaches
  the rendered body, not that the body is a command `lies` accepts.

  The runnable-command test asserts **equality** between each
  vocabulary table and the options the live app declares, in both
  directions. Its first version generated its tails *from the tables*,
  which could only ever catch an invented flag — a flag the command
  declares and the table omits produced no row. That was found by
  deleting `"force"` from the `delete` row and watching the suite stay
  green. Three things are checked, and each has a mutation behind it:
  table equality both ways; that every declared flag actually reaches
  the rendered command (each body carries its own render include-tuple,
  a fourth hand-written table, and dropping `"prompt"` from `new`'s was
  silent); and that a value-taking option has a token after it and a
  required argument has a positional. Two runtime preconditions sit
  *below* the signature and no introspection can see them —
  `new_cmd` raises `BadParameter("library new requires --source")` and
  `lies ingest --source` exits 2 without a collection name. Both are
  body-level refusals with their own regressions.
- **Bodies read the flag collections through `TailParse.flag_on()`.**
  It answers for both `booleans` and `values`, so a body cannot read
  the wrong collection and silently drop a flag. The `=`-attached form
  on a boolean sets the flag and discards the value into
  `ignored_values`; it never smuggles the value into `values`, where
  no body would see it. A *repeatable* value flag reads
  `TailParse.repeats_of()`, which returns every occurrence in order —
  `values` is last-wins, right for a scalar and a silent loss for
  `modify --tag a --tag b`. The backing field is `_repeats`, private so
  the accessor is the only read path.
- **`TailParse.repurposed` is the re-purposed-value set.** An unknown
  flag's following word falls through to `positionals` — on `collections
  show --tag cli`, `cli` becomes the collection name. A *declared*
  boolean followed by a bare word is a genuine surplus positional,
  reported by `_leftover_note` instead.
- **`_refuse_unless_clean` is the one guard, and all seven bodies call
  it.** It refuses on a missing flag value (the command would exit 2)
  and on a repurposed value (the command would carry an argument the
  user never named). Both are refusals rather than notes because both
  make the *rendered command* wrong, and a body appends `note()` to a
  command that exists. The check runs against every positional, not
  only the consumed ones: a surplus value on `delete mylib --tag cli`
  was exactly the case the consumed-only test missed, and it renders a
  destructive verb.

  Five bodies each carrying their own copy of this rule is how the
  class stayed open: four honoured `missing_values`, one honoured
  `repurposed`, and the body with the most flags shipped three exit-2
  renders. A defect class closed once per body is not closed.
  `note()` therefore never claims "no command was run" — four bodies
  append it to a live `Bash(...)`, and the sentence contradicted the
  rest of its own paragraph. `sync` additionally refuses on *any*
  unrecognized flag, because a dropped flag is indistinguishable from
  a request for the whole library — no positional means `lies sync`
  covers every registered collection.
- **`sync` reads its tail as a request, not a flag list.** The body
  carries the whole tail to the agent in a `user_request` verbatim
  fence, the same shape `ask` and `ground` use, and the agent
  identifies the collections the request names — grounding an
  ambiguous name against `collections_read` and asking before
  dispatching. This replaces a positional parse that read one word
  per collection, so `sync please resync my library collections`
  rendered six `Bash(lies sync <word>)` invocations, none of them on
  the collection the user meant. A five-name cap was the
  guard against that; it fixed the paste accident and broke the
  legitimate six-collection request, so both halves are gone. The
  model is asked to *read prose*, never to *parse tokens*: flags
  (`--force`, `--skip-reindex`, `--source`, `--name` and every
  `--no-` form) are still parsed by `_split_tail` and threaded into
  each call the agent dispatches, because a flag is exactly the thing
  a model is worst at spotting in free text.
- **A body renders no placeholder.** A verb that requires an argument
  refuses when the tail omits it (`Cannot run 'where': no collection
  slug was given`) rather than rendering `Bash(lies library where
  '<slug>')`, which exits 2 and reads to the agent as a real argument.
- **`TailParse.note()` returns `""` or a leading-space-prefixed
  sentence.** A body appends it to a rendered command, and a bare
  sentence welds itself onto the last word of that command.
- **`+tag` / `-tag` filter atoms are read from the leading run only.**
  `_parse_question_filters` takes a sigil token as a filter only while
  no question word has been seen yet — the documented shape is
  `+tag -tag <question>` — and only when the atom looks like a tag (a
  `:` qualifier, or two or more characters). Both guards exist because
  this library indexes command-line tooling, where a question *about*
  option flags ("the `-e` flag of grep") is ordinary English; without
  them the parser deletes those words and re-injects them as
  `exclude_tags`.
- **`ground` parses flags from the leading run only, and `ask` parses
  no flags at all.** Both tails are mostly a question, so running the
  full flag grammar over them deletes words a question about option
  flags is made of. `_split_leading_flags` stops at the first non-flag
  token and honors a `--` terminator; the body names the limit when the
  grounded question contains a flag, so the agent can tell the user
  why the words came back inside the search string.
- After the answer, a `MemoryEnricher` sub-agent proposes a structured
  `MemoryPlan` only when evidence warrants it.
- The `EnrichmentQueue` (in `src/lies/memory/retry.py`) is a per-session,
  in-memory FIFO that retries transient `WikiMemoryService.apply_plan`
  failures (`WikiLockBusy`, `WikiWriteConflict`, `WikiCommitFailed`) at the
  start of the next turn. Capped at 3 attempts; deferred items surface as
  `(memory: deferred after 3 attempts — <reason>)` in the next receipt.

The lint repair workflow uses a separate `repair_agent` (in `src/lies/agents/repair.py`) that consumes a `LintReport` and emits a structured `RepairPlan`. The orchestrator applies the plan through `WikiMemoryService.apply_repair_plan`, which routes through the same cross-process flock and atomic-commit envelope as memory plans. The 4 primitives (`CreateStub`, `AppendLink`, `UpdateIndex`, `AppendEvidence`) map onto existing memory operations. The agent never emits ops for `safe_to_fix=False` findings; those stay in the report verbatim. The CLI flag is `lies lint --fix`; the FastMCP toggle is `lint(fix=True)`.

The lint pass composes the deterministic shell (`_build_lint_report`, covering orphan + missing_xref + missing_page) with the linter sub-agent's structured `LintReport` (covering contradiction + stale + data_gap + its own mechanical findings). `merge_lint_reports` unions the two with a `(category, pages, message)` dedup key; the shell wins on collision so the deterministic `safe_to_fix` semantics for mechanical categories are preserved. The LLM sub-agent is fail-soft; when it raises, the shell's findings still reach the repair agent.

## Data shapes (Tier 2 query path)

The retrieval → synthesis → filing-back path threads five data shapes.
All five are forward-only (additive fields default to safe sentinels;
no field has been removed from public surfaces, though internal
helpers `_first_meaningful_paragraph` is gone and `_extract_section_at`
is deprecated in favor of the span parser):

- **`Span`** (`src/lies/markdown_spans.py`, F37). Frozen dataclass
  carrying `(heading_path, body, code_fence, start_line)`. `Span.body`
  is the raw text between heading boundaries; `heading_path` is the
  ordered list of `## …` / `### …` headings from the page root down
  to the span's section; `code_fence` flags spans whose body sits
  inside a fenced code block (downstream consumers exclude these
  from prose excerpts); `start_line` is the 1-indexed line in the
  source text where the span begins. `parse_spans(text) -> list[Span]`
  is the single parser entry point.
- **`PageRead.spans: list[Span]`** (`src/lies/memory/retrieval.py`,
  F19). Replaces the prior `PageRead.excerpt`; retrieval populates
  the field by calling `parse_spans(content)` at read time. The
  orchestrator and the synthesizer consume only `spans` going forward.
- **`Citation.heading_path: list[str] | None`** (default `None`,
  `src/lies/query/citation.py`, F19). Populated by
  `_thread_heading_paths` from the `Span` each claim cites. Additive —
  existing citations that the synthesizer never threaded a path for
  carry `None`. The MCP `query` envelope and the `lies query` JSON
  output surface this field alongside the existing `path`, `line`,
  `section`, and `source` discriminator.
- **`ClaimCitation.quote: str`** (default `""`, F19). The verbatim
  excerpt from the cited span body. Validated by
  `_validate_claim_citations` to appear as a substring of the cited
  span's body; rejected claims surface a synthesis-time warning.
- **`LibrarianDeps`, `PageExcerpt`, `LibrarianOutput`**
  (`src/lies/agents/librarian.py`, F18). Pydantic-ai dataclasses for
  the librarian subagent's deps + output contract.
  `LibrarianDeps` carries the search/read tools; `PageExcerpt` is
  one retrieved page (slug, heading_path, body excerpt, code_fence
  hint); `LibrarianOutput` is the curator's bundle of `PageExcerpt`s
  passed to the synthesizer.

Filed synthesis pages (F19 filing-back) render the `## Evidence`
section using the `[[slug]] (Heading > Subheading): "verbatim"`
form. The `_render_evidence` helper guarantees the section exists
and is well-formed per the F17 page-type schema contract. Spec:
`~/code/project-notes/lies/superpowers/specs/2026-09-19-tier2-query-path-design.md`.

## Grounding archivist

`src/lies/mcp/grounding.py` exposes a tight, snippet-only view of the
corpus so the agent can verify coverage before reasoning. It reuses
the F18 librarian (shipped at v0.33.0) — `ground()` calls the
librarian, trims each excerpt to ≤200 chars at a word boundary, and
returns the bundle as an `ArchivistDigest` shaped for
`[[slug]]: "snippet"` rendering (NOT F19's long
`[[slug]]: "verbatim"` form).

The module exports:

- **`CitationSnippet(collection, slug, title, snippet)`** — frozen
  dataclass, one entry per retrieved excerpt. `collection` is
  `"wiki"` or a library-collection name; `slug` is the bare slug
  (e.g. `"concepts/pydantic"`); `snippet` is the first ≤200 chars
  of the first prose span.
- **`ArchivistDigest(question, tag_expr, exclude_tags, citations,
  no_coverage, distinct_pages)`** — frozen dataclass. `no_coverage`
  is true only when the librarian dispatch fails; the F15 coverage
  gate is the typed `ArchivistCoverageError` raised on unknown
  include tags (translated to `ToolError` at the MCP layer).
- **`ArchivistCoverageError`** — raised on unknown tag or unparseable
  include expression. The MCP `ground` tool catches it and re-raises
  as a `ToolError` so LLM callers can react.
- **`truncate_at_word_boundary(text, max_chars)`** — cuts at the last
  whitespace ≤ `max_chars`; hard-cuts when the candidate has no
  whitespace. The library is `str.isspace()`-aware (handles spaces,
  tabs, newlines, and other ASCII whitespace classes).
- **`pick_first_prose_span(spans)`** — first non-code-fence,
  non-empty `Span` from the F37 span list.
- **`ground(question, tag_expr, exclude_tags, top_k)`** — top-level
  orchestrator. `top_k` is clamped to `[1, 10]`. No LLM call in this
  module; the librarian owns the model dispatch.

The MCP tool (`@mcp.tool(name="ground")` in `src/lies/mcp/server.py`)
wraps `ground()` and returns the digest via `dataclasses.asdict` for
JSON-serializable wire format. Spec:
`~/code/project-notes/lies/superpowers/specs/2026-09-20-grounding-archivist-design.md`.

## Dual-source routing

The librarian (`src/lies/agents/librarian.py`) and the archivist
(`src/lies/mcp/grounding.py`) retrieve from BOTH surfaces in
parallel:

1. **Library collections** at
   `~/.local/share/lies/library/collections/<name>/` — primary
   source, authoritative.
2. **Wikis** at `~/.local/share/lies/<name>/` — secondary source,
   derived.

`CitationSnippet.source_kind: Literal["library", "wiki"]`
records which surface produced each snippet. Library-wins-on-
conflict drops the wiki copy entirely on slug match, so there is
no merged-row third value.

**Library-wins-on-conflict:** when the same `slug` exists in both
surfaces, the library hit replaces the wiki hit. The wiki copy is
dropped entirely; the merged hit carries `source_kind="library"`.
This rule was previously applied at synthesis time; dual-source
routing applies it at retrieval time.

The librarian queries the library's qmd index at `lib.git_root` (with
a `collection_filter` of registered library names), not at any wiki's
`wiki_dir`. Library collections and wiki pages share the slug space
but live in separate qmd indexes.

**Render marker:** when the LLM renders the archivist's digest as
citation lines, wiki-only hits (where `source_kind="wiki"`) are
prefixed with `[secondary] ` to flag that the snippet is not
grounded in a primary source. Library hits render unprefixed.

```
[[mermaid/syntax/flowchart]] (Flowchart syntax): "flowchart TD; A-->B"     # library (primary)
[secondary] [[default/concepts/pydantic]] (Pydantic concept): "..."      # wiki-only (secondary)
```

**Read-side dispatch:** `_wiki_read` is source-aware. Wiki page IDs (`page-` + sha1-12) route to `memory_service.read()`. Library paths (`<collection>/<page>`) read from the library's qmd chunks via `qmd_get(library_git_root(), "qmd://<path>")`. Library hits carry `page_id=None` so the calling LLM doesn't try to read them via the wiki service.

## Quality gates

`make check` runs `lint + typecheck + format`. `make test` runs the full
pytest suite. Pre-commit hooks wrap the same targets so a commit that
lands in the repo has already passed all gates.

The per-test budget gate (`tests/unit/conftest.py`) fails a non-slow-marked
test whose call phase exceeds 0.15s — but only after re-running the
breaching test **in isolation**. A full-suite run measures every test
under contention, and the threshold sits close enough to the scheduler's
noise floor that instantaneous tests get flagged for latency rather than
for cost. A breach that clears on re-measure is reported as noise and the
run passes. Reaching for `@pytest.mark.slow` to silence a gate failure
removes the test from the default run rather than fixing anything; mark
only what genuinely costs more than the budget.

## References

- Project overview: README.md
- Makefile targets: Makefile
- Release notes: CHANGELOG.md
