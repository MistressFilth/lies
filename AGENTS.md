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

   **A tool *response field* is not a tool signature.** The major
   rule is about signatures: a parameter removed, a return type
   changed, a tool dropped. Adding a field to a response envelope is
   additive and takes a minor, even when an existing field's
   *semantics* shift. The 0.47.0 release is the worked example:
   `search` gained `transient`, and `no_coverage` stopped being set on
   a dispatch failure. Both are additive or corrective — the old
   `no_coverage=True` on a timeout violated the field's own documented
   meaning ("the corpus has zero hits"), so the change moved the code
   onto the contract rather than away from it. A caller that read
   `no_coverage` alone is not broken; it stops being told a falsehood.

   The distinction that decides it: ask whether a caller following the
   *documented* contract still works. If yes, minor. If the caller
   has to change code to keep working, major.

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
│   ├── access.py    # THE SEAM: DAEMON_TOOLS / CLI_ONLY_OPS, daemon_tool(),
│   │                #   QmdDaemonUnavailable / QmdDaemonWedged, classify_call_error()
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
  Routes through the qmd access seam
  (`lies.qmd.access.daemon_tool("query", ...)`) — a single daemon
  `query` against the resolved collection set with the daemon's
  `collections` push-down. The pre-#106 fan-out was a per-collection
  CLI subprocess loop that ranked globally and could starve a
  multi-collection query to zero rows even when it had matches; the
  daemon's `collections` parameter is a true push-down and returns
  in-scope rows from every named collection in one round trip.
  The seam also owns the recycle-and-raise contract
  (`QmdDaemonWedged` carrying the daemon's last log tail,
  `QmdDaemonUnavailable` for the operator), and `ground()` re-raises
  both — a process failure is no longer folded into `no_coverage=True`.
  Per-call timeout comes from
  `lies.config.get_qmd_query_timeout` (default 60s, override
  `LIES_QMD_FANOUT_TIMEOUT`), read at call time and shared by every
  qmd *retrieval* call site — the fan-out, the `search` tool, and the
  agent path's HTTP transport all read it. The seam forwards the
  timeout to `fastmcp.Client.call_tool` as a per-call deadline, so
  a change takes effect on the next call without waiting for the
  cached httpx client to be invalidated. Liveness probes in
  `qmd.lifecycle` / `qmd.daemon` keep their own short deadlines and
  must not be widened: a slow answer to "is this alive?" is the
  failure. The pre-#106 consecutive-error recycle counter is gone
  because the seam does its own recycle and the seam's typed errors
  propagate to the archivist unchanged.

- **A qmd timeout is a slow daemon, not an unreachable one, and not
  a statement about the corpus.** `QmdTimeoutError` is a distinct
  subclass of `QmdCommandError`, and `search` maps it to
  `transient=True` with `no_coverage=False`. The distinction is the
  whole point: `no_coverage` means *this search found nothing*, and
  the librarian contract tells the model that flag means "the corpus
  has zero hits for this question". A timeout used to set
  `no_coverage=True` and was labelled `qmd unreachable`, so an
  intermittent stall reached the user as "No relevant content found
  in library." — a false claim about the corpus, for a query that
  answered in under six seconds on the retry. `QmdTimeoutError`
  carries qmd's captured `stderr` for the same reason: the deadline
  message is a constant, so a timeout without it arrived with no
  evidence of where the time went.

  Measured against the live 5987-doc corpus (2026-10-01): warm
  5.6–6.0s, three concurrent clients 5.9–6.6s, no timeouts in ~150
  calls. Stalls past 15s do occur under host contention, and the
  trigger was not isolated — hence 60s and a knob rather than a
  tighter budget justified by a cause nobody has found.
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

## The qmd access seam (`qmd/access.py`)

One module owns every call LIES makes to qmd and encodes which
transport serves which operation. Read it before adding any qmd call.

- **Routing is by capability, never by availability.** `DAEMON_TOOLS`
  is what qmd's MCP server exposes (`query`, `get`, `multi_get`,
  `status`); `CLI_ONLY_OPS` is everything else, including BM25
  `search`, which the daemon has no path for. A down daemon raises
  `QmdDaemonUnavailable` naming `lies qmd up` and `LIES_QMD_URL`.
  There is no degraded mode, no empty result, and no CLI fallback —
  the previous behaviour reported an unreachable daemon as "no
  relevant content in the library", a claim about the corpus that was
  really a claim about the process.
- **The CLI half is not only maintenance.** `CLI_ONLY_OPS` also
  carries the *diagnostic* surface the daemon has no path for, and
  each of these is reachable from the product:
  `doctor` (index + collection health), `ls` (inspect indexed files),
  `bench` (score a known-answer fixture), `cleanup` (reclaim orphaned
  index rows), `collection` (registry CRUD), and `mcp` (the daemon
  lifecycle itself). `search` sits there because BM25 is a CLI path.
  A capability unlocked on the CLI and left undocumented reads as
  unimplemented, so `tests/unit/test_agents_claims.py` checks this
  list against the module.
- **`lies mcp up` is not the fix.** It starts LIES' *own* MCP server
  (`mcp/daemon.py`). The qmd daemon is `lies qmd up` (`cli/qmd.py`).
  An operator who follows the wrong one changes nothing and never sees
  the real cause.
- **Three daemon failure modes, three answers.** A wedge (accepted the
  call, then stopped answering) recycles and raises
  `QmdDaemonWedged` carrying `last_output`, the tail of qmd's own
  `mcp.log`; no transparent retry, because a fresh daemon re-wedges on
  the same payload. Unreachable recycles and retries once. A
  protocol-level rejection is not a transport failure and re-raises
  unchanged. `classify_call_error` is the only place that decides.
- **`last_output` must come from the daemon that actually wedged.** The
  two sites read on opposite sides of their recycle, and both are
  correct. The first call wedges → the recycle below spawns its
  replacement and qmd truncates `mcp.log` on every start, so the read
  comes *before*. A retry wedges → the recycle above already started
  that daemon and it has been logging since, so the read comes
  *after*. The question is never "which side of the recycle am I on"
  but "which daemon wedged"; a tail attributed to the wrong daemon is
  worse than no tail, because it is confidently wrong.
  `test_each_wedge_carries_the_log_of_the_daemon_that_actually_wedged`
  pins both, and a mutation that "harmonises" the second site with the
  first is caught by it.
- **A retry's failure is classified, not assumed.** After a recycle the
  second failure takes one of three routes: a reason the taxonomy does
  not own propagates unchanged; a re-wedge raises `QmdDaemonWedged`;
  only still-unreachable raises `QmdDaemonUnavailable`. Collapsing them
  tells the operator to start a daemon that is already running. The
  agent path's `QmdRecycleToolset` does the same on its retry, so a
  decode error is not reported to the model as *unreachable*.
- **Deliberate taxonomy gap.** `httpx.HTTPStatusError` (a 5xx from a
  daemon failing internally) and `httpx.LocalProtocolError` (an
  illegal header value, unsupported URL scheme, or other client-side
  malformed request — a retry sends the same bytes back to fail the
  same way, and a recycle kills in-flight work belonging to other
  clients of a machine-global daemon) classify as `"passthrough"`
  and get no recycle. `httpx.RemoteProtocolError` (the daemon's
  response was malformed — server-side state) keeps the
  `"recycle-retry"` path through `_TRANSPORT_NAMES`. All three classes
  are pinned in `tests/unit/qmd/test_access.py` (the
  `RemoteProtocolError` and `LocalProtocolError` tests are the I-4
  pin) so a future match-by-name rewrite cannot silently re-merge the
  client-side case. Recorded at the classifier so it reads as
  considered.
- **The taxonomy matches exception class *names*, not httpx types.**
  fastmcp 4 vendors its own httpx as `httpx2`, and `httpx2.ReadTimeout`
  is not a subclass of `httpx.ReadTimeout`; a dead session additionally
  arrives as a bare `RuntimeError("Client failed to connect: ...")`
  with the real error on `__cause__`. A taxonomy written against the
  `httpx` LIES declares matches none of them, so every wedge silently
  becomes a passthrough and no recycle ever runs. The three shapes the
  installed fastmcp actually raises are pinned in
  `tests/unit/qmd/test_access.py`; re-derive them by probe before
  changing the matcher.
- **`_build_qmd_httpx_client` takes `**kwargs` for a reason.** fastmcp
  calls it with `follow_redirects=`; a fixed signature made every HTTP
  daemon call fail at connect with a `TypeError` before any of the
  above ran. It also builds its client from the httpx generation
  fastmcp installed, and reads its read deadline from
  `get_qmd_query_timeout()` so the daemon and the CLI cannot answer
  differently for the same retrieval.
- **The cached client is a client, not a session.** It is cached
  per-process so the daemon's model stays warm (3.11s against 10.77s
  cold), keyed on the URL so a changed `LIES_QMD_URL` rebuilds it. An
  MCP session is bound to the event loop that opened it and this seam
  is called from `asyncio.run` bridges, so the session is per call.
- **`daemon_tool` returns the raw `CallToolResult`.** `get` and
  `multi_get` answer with a content block and `.data` is `None`; a
  caller that reaches for `.data` stores an empty body, which is the
  exact failure the routing work exists to prevent. Note also that the
  daemon answers an *unknown collection* with an empty result and **no
  error** (the CLI exits 1 on the same class), so the `isError` branch
  is for genuine tool errors only — validate scope against the registry
  before dispatching.
- **`validate_scope(scope)` is the shared pre-check.** `search` and
  `ground` both issue `query` calls with a batched `collections`
  array. The daemon answers an unknown name with an empty result and
  no error, so a single unresolvable name inside the batch silently
  returns zero rows. `validate_scope` reads the daemon's `status`
  tool, partitions the input into served + absent names in input
  order, and raises the typed errors `daemon_tool` raises. `search`
  refuses on `unknown_tags`. `ground` reports the split on the
  digest instead: `searched_scope` carries what was dispatched and
  the new `unserved_scope` field names what the daemon does not
  serve, so neither list can assert a collection the call never
  reached. A fan-out whose `searched_scope` is empty searched
  *nothing*, which is not a statement about the corpus, and says
  so with `no_coverage=False`; the unserved names are logged at
  warning. Without the pre-check, the per-collection fan-out the
  prior shape dropped individually becomes a clean-miss claim about
  the corpus on the batched path. Pinned by five tests in
  `tests/unit/qmd/test_access.py::test_validate_scope_*` and three
  in `tests/unit/mcp/test_ground.py` that cover the unserved set.
- **The default daemon URL carries `/mcp`.** `DEFAULT_QMD_URL` is
  `http://127.0.0.1:8181/mcp`, not the bare origin. qmd serves exactly
  one route, and a URL without the path reaches a *live* daemon and
  comes back 404 — which the taxonomy reads as a transport failure and
  reports as a down daemon, advising the operator to start a daemon
  that is already running. `qmd.lifecycle` always built the URL with
  the path; the config default had not caught up. The other two
  spellings of this URL — `QmdCapability.__init__`'s `url` default and
  `QmdMcpClient.url` — now *source* the constant rather than repeating
  it, so a fourth spelling cannot appear.
- **Every qmd subprocess runs with `NO_COLOR=1`, as policy rather
  than as a fix.** qmd's only `NO_COLOR` consumer is
  `dist/cli/qmd.js:92`, `useColor = !NO_COLOR && process.stdout.isTTY`
  — and LIES always pipes, so colour is already off and this override
  cannot change today's bytes. It is defence in depth: it removes qmd's
  colour output as a variable, and it forces rather than inherits so an
  operator who exported `NO_COLOR=0` to re-enable colour in their own
  shell cannot change what `qmd_query` parses. Do not cite it as the fix
  for anything.
- **The `⠋ Gathering information` spinner is `ipull`, not qmd — and
  only during a model download.** qmd 2.5.3 has no spinner: no
  `Gathering information` or `⠋` anywhere in the package, no
  `ora`/`clack`/`cli-spinners` dependency, and its only cursor control
  is `hide()` at `qmd.js:105`, which writes `\x1b[?25l` to **stderr**.
  The spinner comes from `ipull` (a transitive dependency of
  `node-llama-cpp`, which qmd uses for its models) driving
  `stdout-update`, whose `UpdateManager.getInstance()` defaults
  `stdout = process.stdout` and writes there with **no TTY guard**. It
  therefore *does* land on stdout and *can* corrupt `qmd_query`'s
  `json.loads` — but only while qmd is downloading a model into a cold
  cache, which is why it presented as intermittent.
  `NO_COLOR` does not suppress it; `stdout-update` never reads it.
  An earlier version of this file claimed qmd wrote the spinner to
  stdout while gating on `stderr.isTTY`. That was wrong on both halves
  and was corrected against the installed package. **If you need this
  suppressed, the lever is a warm model cache**, not an env var — the
  production answer is `lies sync` having embedded already.
- **The idle bound is right for queries and wrong for `embed` and
  `update`.** The wedge detector fires after 30s of silence, which is
  exactly right for an interactive query. But two commands are silent
  for their *whole* duration, and there the silence is the operation,
  not a symptom: under a pipe `qmd embed` writes exactly one byte (its
  spinner escape, `dist/cli/qmd.js:105`, which is stderr) and then
  nothing while the model loads and runs, and `qmd update` writes
  nothing at all — its progress is a stderr write behind an `isTTY`
  check (`qmd.js:552-566`). Measured: 9.4s of unbroken silence for one
  tiny embed on a cold cache, and four collections under host contention
  crossed 30s and were killed mid-progress.
  `qmd_embed` and `qmd_update` therefore pass
  `idle_timeout=timeout * SILENT_COMMAND_IDLE_TIMEOUT_FRACTION`;
  `_run` still defaults to `DEFAULT_IDLE_TIMEOUT_S`.
- **That fraction is 0.5, not 1.0, and the reason is load-bearing.**
  The reader loop checks the *total* bound first, so an idle bound equal
  to the total can never fire: every such kill would report
  `bound="total"` and the `last_output` tail — the only evidence of
  where the time went — would be lost. At half the total, a genuinely
  hung command is still caught by the idle bound and keeps its
  diagnostic. The cost is stated rather than hidden: these commands hold
  `with_qmd_lock()` for the whole run, so a wedged one now holds the lock
  for up to half its total bound instead of 30s. That is the trade — the
  alternative was killing healthy long-running work, which is worse, and
  the lock is only contended by other qmd operations from this process.
  **When adding a qmd command, ask whether it talks while it works.** If
  it does not, its idle bound has to follow its total bound or it will be
  killed for making progress.

## The read tool's bodies (`mcp/read.py`)

`read` is where F19's citation contract is met or missed, so two of its
properties are load-bearing rather than incidental.

- **The body is the document and nothing else.** A citation is
  `[[slug]]: "verbatim quote from the cited span"`, so the library
  branch issues the daemon's `get` with `lineNumbers: false`. The CLI's
  `qmd get` cannot supply this: it line-numbers every line by default
  and `--no-line-numbers` still leaves its `qmd://path  #docid` header.
- **One `get` per path, never `multi_get` for a batch.** `multi_get`
  *skips* (does not truncate) any file over its 10KB default, and 1854
  of this corpus's 5987 documents are over it. A batched read would
  silently drop nearly a third of what a reader can ask for, and would
  also collapse on a single unresolvable entry. `get` has no size cap
  and one failure per call. Measured: a warm `get` is 0.07s against the
  live daemon, so the round trip batching would save is not worth the
  corpus it loses.
- **One MCP session per batch, via `read_library_bodies`.** The
  previous shape opened a fresh `async with client:` per path and
  paid a ~33 ms handshake each time. Measured against the live
  daemon (2026-10-03, 10 warm samples, `claude_code/concepts/hooks.md`):
  one-shot session p50 75.4 ms; persistent-session `call_tool`
  p50 41.9 ms. A 20-path read drops from ~1.5 s to ~900 ms.
  `read_library_bodies(paths)` opens one session and issues one
  `get` per path under it; per-path failures (`RuntimeError` on a
  missing document, `is_error=True`, notice-only `content`) map
  to `None` in the output list, and only `QmdDaemonUnavailable`
  / `QmdDaemonWedged` short-circuit the batch.
- **A notice is not a body.** `multi_get` reports a skipped file as
  `[SKIPPED: …]` and an unresolvable entry as `Errors:\nFile not
  found: …`, both as TextContent blocks alongside the bodies.
  `_resource_texts` and `_notices` keep them apart, and a result with
  no resource block raises rather than returning `""`.
- **Who owns the failure decides whether it is skippable.** A daemon
  that is down or wedged re-raises: swallowing it turns a reachable
  failure into `ToolError("all reads failed")`, a claim about the corpus
  that is really a claim about the process.
- **Both spellings of "no body for this path" are skipped together.**
  qmd says it two ways — the call raises (`Document not found`), or the
  call succeeds and returns only notice blocks. An earlier version
  treated the second as fatal and ran extraction *outside* the
  per-path `try`; because the exception propagated, the partially-filled
  result was discarded, so one anomalous document silently cost the
  caller every good body in the batch. Do not branch on which channel
  qmd used: that is an implementation detail of its error signalling,
  and a batch's outcome must not depend on it. `ToolError("all reads
  failed")` is the loud failure, raised once, when the batch genuinely
  produced nothing.
- **Partial batches carry the unresolved paths on the wire.** A
  20-path read where 3 paths could not be resolved returns a
  17-body dict *plus* the synthetic key `"_missing"` whose value is
  a list of the three paths in input order. The `"_"` prefix
  keeps the signal out of the path space (library paths are
  `<collection>/<page>`, wiki IDs are `page-…`); the field's type
  is a list so a caller iterating `out.items()` can filter with
  `key.startswith("_")` if it wants the prior shape. The loud
  all-or-nothing `ToolError("all reads failed")` is preserved for
  the all-fail case. Without this, the previous shape returned a
  17-key dict with `log.warning` lines that did not reach the
  agent — the I-9 partial-batch class.
- **The sync bridge runs its own loop when one is already running.**
  `_read_impl` is sync (the `Tool.from_function` registration and
  `server.py` both assume it) while `daemon_tool` is async.
  `asyncio.run` from a thread that already has a loop raises
  `RuntimeError` — the bug `ground()` shipped with in #106 — so that
  case gets a dedicated thread and its own loop instead of an error.

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
  no_coverage, distinct_pages, searched_scope, no_library, transient)`**
  — frozen dataclass. `no_coverage` is true only when the librarian
  dispatch *succeeds* and returns zero hits (or when the library is
  uninitialized — `no_library=True`). `transient` (added in 0.46.0)
  is the new flag for *dispatch* failures: a fan-out ``Exception``,
  a tagged fan-out ``Exception``, or a librarian ``Exception``
  sets ``transient=True, no_coverage=False`` so the caller can
  distinguish "the daemon failed" from "the corpus has nothing".
  Defaults to ``False``; existing call sites that build a digest by
  keyword remain stable. The F15 coverage gate is the typed
  `ArchivistCoverageError` raised on unknown include tags
  (translated to `ToolError` at the MCP layer).
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

**Read-side dispatch:** the `read` tool is source-aware. Wiki page IDs (`page-` + sha1-12) route to `memory_service.read()`. Library paths (`<collection>/<page>`) route to the qmd daemon's `get` with `lineNumbers: false`, and the body is read from the content block rather than `.data` — see "The read tool's bodies" above. Library hits carry `page_id=None` so the calling LLM doesn't try to read them via the wiki service.

## The ingest idempotency check has three states, not two

`library/ingest.py::_process_item` compares the incoming
`source_hash` against the existing mirror's frontmatter. A mirror
carrying a matching hash is a **skip**; a mirror carrying a different
hash is a **conflict**; a mirror carrying *no* `source_hash` is neither.
It was written as one yes/no question —

```python
if existing_hash and existing_hash == item.source_hash:  # skip
...                                                     # quarantine
```

— so the third state fell through to the second and was reported as a
disagreement with an empty left-hand side printed into the message:
`mirror-collision:page:existing-!=new-<hash>`. A hand-written page
sitting where a mirror would sit produced that on every run, against a
perfectly ordinary outside source. It is now
`mirror-unmanaged:<slug>:no-source-hash`; the outcome is unchanged
(fail loud, preserve the page) and a real mismatch keeps its
`mirror-collision` name. Pre-existing since #62.

**The lesson is the shape, not the string.** A guard written as
`if x and x == y` has two states on paper and three in the domain, and
the third is always the one nobody writes a test for.

A **self-ingest** — `--batch` pointed at a collection directory, the
obvious way to "re-sync" one — is the degenerate case of that third
state, and is now refused outright (`SelfIngestRefused`, exit 2). The
source and the mirror target are the same path, because the slug is
derived from the source's relative path and the target is
`coll.dir / f"{slug}.md"`, and `derive_nested_slug` mirrors that path
segment for segment. It can only ever "skip everything" or "rewrite
everything"; measured, it did neither — three runs, `errors=1` each,
nothing written.

**This is the caller mistake that produced the 36 stray `config.md`
pages** (fixed in 0.48.2 by gating the walk on document suffixes). That
release stopped the config becoming a page and left the no-op in place.
When reading a 0.48.2-shaped ingest bug, check the direction of
`--batch` before the filter.

The same 0.48.2 report described the symptom as "`updated=1` on every
run, never `skipped`". That is `--force`, which skips the check
entirely (`if existed and not force:`) and works as documented. A
reproduction that uses `--force` measures the flag, not the code.

## The qmd index is keyed by `$XDG_CACHE_HOME`, not `$XDG_DATA_HOME`

`qmd` keeps its index in one machine-shared file, `~/.cache/qmd/
index.sqlite`, resolved from `$XDG_CACHE_HOME`. A sandbox that redirects
the library root sandboxes the mirror files and nothing else:

```
$ XDG_DATA_HOME=/tmp/sandbox-data qmd --help | grep ^Index:
Index: /home/divinefilth/.cache/qmd/index.sqlite
```

`tests/conftest.py::_isolated_xdg` redirects every root, which is why
the suite never hit this and why a hand-rolled sandbox does. The qmd
write helpers (`update`, `collection add` / `add_if_missing` / `remove`
/ `add_or_update`, `embed`, `cleanup`, `reindex`) refuse the
data-only combination and name both variables; reads are unguarded,
because the daemon is machine-global and serving the live index to
every client is the only configuration that is safe.

## Known flakes

Measured failures that are real and are not a bug in LIES. Recorded
here so the next agent does not spend a cycle rediscovering them — and,
more importantly, does not try a mitigation that was already measured
and rejected. Where a fix exists it is named; where none does, that is
stated rather than left to be re-derived.

### `qmd embed` aborts on the CUDA VMM reservation

node-llama-cpp intermittently hard-aborts inside `qmd embed`:

```
[node-llama-cpp] CUDA error: out of memory
[node-llama-cpp]   current device: 0, in function alloc at
  .../ggml/src/ggml-cuda/ggml-cuda.cu:492
[node-llama-cpp]   cuMemAddressReserve(&pool_addr, CUDA_POOL_VMM_MAX_SIZE, 0, 0, 0)
... ggml_abort -> SIGILL
```

**"Out of memory" is a false label.** The call reserves *virtual address
space*, and `CUDA_POOL_VMM_MAX_SIZE` is hardcoded at **32 GB**. Measured
on this host (RTX 4090, 24 GiB): peak embed usage was **4775 MiB at 38%
utilisation with ~20 GB free**. The reservation fails with memory to
spare — it is an address-space reservation failing, not VRAM exhaustion,
and no amount of free VRAM changes the outcome.

**It is WSL2-specific.** This host is WSL2 (`6.18.33.2-microsoft-standard-WSL2`,
`WSL_DISTRO_NAME=Ubuntu`, driver 616.64, `/usr/lib/wsl/lib/libcuda.so.1.1`),
so CUDA runs on the same WDDM-backed stack as native Windows. Upstream
`withcatai/node-llama-cpp#580` reports the identical failure on Windows
CUDA (RTX 3090, same 32 GB reservation, same hard abort, same qmd
workload) and states the reservation fails "even with plenty of actual
VRAM available". The runtime fallback requested there is
`withcatai/node-llama-cpp#610` — **still open**, which is why a build
flag is currently the only lever.

**Why qmd's own guard misses this platform.** qmd pins embedding
parallelism to 1 for exactly this failure mode, but keys it on
`process.platform`:

```js
// node-llama-cpp/llama.cpp CUDA on Windows is unstable with multiple
// simultaneous contexts (ggml-cuda.cu:98 in #519). Vulkan and CPU do not
// show the same failure mode, so only serialize Windows CUDA by default.
if (platform === "win32" && gpu === "cuda") return 1;
```

Under WSL2 `process.platform` reports **`linux`**, so the one platform
running the vulnerable stack is the one platform this never fires on.
That guard is *not* the fix, though — see the parallelism note below.

#### The fix: rebuild the CUDA backend with `GGML_CUDA_NO_VMM=ON`

`GGML_CUDA_NO_VMM` is a CMake option (`ggml/CMakeLists.txt:202`, default
`OFF`) that `#define`s out the VMM pool (`ggml/src/ggml-cuda/common.cuh:189,221`)
and falls back to the legacy pool. Verified on the artifact:

| check | stock | rebuilt |
|---|---|---|
| `ggml_cuda_pool_vmm` symbols | 18 | **0** |
| `cuMemAddressReserve` references | 18 | **0** |
| undefined CUDA **driver** symbols | 11 | **0** |

The driver-symbol drop is the clean confirmation: the VMM pool was the
*only* consumer of the CUDA driver API in the backend, so compiling it
out removes the `libcuda.so.1` dependency entirely. Everything else runs
on the CUDA runtime API (`libcudart.so.13`).

Measured on `tests/integration/test_tag_filter_end_to_end.py` (8 tests
x 4 collections of fresh embeds per run), alternating backends so host
drift cannot masquerade as an arm difference:

| arm | suite runs | runs containing ≥1 CUDA abort |
|---|---|---|
| **NO_VMM (rebuilt)** | 5 | **4** |
| **stock** | 4 | **3** |

**The rebuild is NOT a fix.** Both arms abort at the same rate, and a
standalone run verified patched immediately before *and* after still
produced 2 aborts. The library was patched; the process did not care.

**Why: the wrong file was patched.** `qmd` loads llama.cpp through
`node-llama-cpp`, but that is not the only CUDA backend on this host.
`/usr/local/lib/libggml-cuda.so` — a **separate llama.cpp build** —
carries `ggml_cuda_pool_vmm` (18 symbols) and `cuMemAddressReserve`
(3 references), and is registered with the dynamic linker:

```
$ ldconfig -p | grep libggml-cuda
	libggml-cuda.so   (libc6,x86_64) => /usr/local/lib/libggml-cuda.so
	libggml-cuda.so.0 (libc6,x86_64) => /usr/local/lib/libggml-cuda.so.0
```

A `dlopen` resolved by SONAME can land there instead of
`node-llama-cpp`'s copy, and `/usr/local/lib/libggml-cuda.so.0` was
never patched. The backtraces name `libggml-base.so` frames throughout,
which was a further hint that the faulting object was not the file being
edited.

The error generalises: the artifact was verified **built and correct**,
but never verified **loaded**. Patching a library and checking the file
is not evidence that any process used it.

#### Which library actually aborts — the file to patch

**`node-llama-cpp` ships two CUDA variants and this host loads neither of
the obvious ones.** `@node-llama-cpp/linux-x64-cuda` and
`@node-llama-cpp/linux-x64-cuda-ext` both expose an identical
`getBinsDir()`, so the parent package selects one at runtime. On this
host it selects **`linux-x64-cuda-ext`**, whose `bins/` directory
contains **only** `fallback/`:

```
~/.bun/install/global/node_modules/@node-llama-cpp/
  linux-x64-cuda/bins/linux-x64-cuda/          <- the OTHER variant
      libggml-base.so, libggml.cuda.b8390.so, libllama.cuda.b8390.so
      libggml-cuda.so            (464 MB is NOT here; ~59 MB build)
  linux-x64-cuda-ext/bins/linux-x64-cuda/fallback/
      libggml-cuda.so            <- 464 MB, 18 ggml_cuda_pool_vmm symbols
```

Every other library — `libggml-base.so`, the `*.b8390.so` shims — still
comes from `linux-x64-cuda/bins/`, which is why the backtraces name
`libggml-base.so` under that path while the CUDA backend itself came
from the other variant's `fallback/`. **Patching
`linux-x64-cuda/bins/linux-x64-cuda/libggml-cuda.so` changes nothing
on this host.** Seven rebuild attempts failed for exactly that reason.

Two ways to find the real file, both of which a directory glob misses:

```bash
# the directory contains only `fallback/`, so `bins/*/*.so` finds nothing
find ~/.bun/install/global/node_modules/@node-llama-cpp -name '*.so*' -type f \
  | while read -r f; do
      n=$(strings "$f" 2>/dev/null | grep -c ggml_cuda_pool_vmm)
      [ "$n" != 0 ] && echo "$n  $f"
    done
```

**The fix is still unvalidated.** Dropping a rebuild into that path did
remove the abort across three suite runs — and the same runs printed
`QMD Warning: no GPU acceleration, running on CPU (slow)`, then failed
with `QmdTimeoutError` at the 60s deadline. The original is a 464 MB
fat binary; an `sm_89`-only build loads but does not bring up CUDA
acceleration there. **The abort disappeared because the GPU was turned
off, not because VMM was removed.** A real attempt must rebuild the
variant node-llama-cpp actually bundles, at the same commit, as a fat
binary, and re-check that GPU acceleration survives.

#### Re-arming the backend after a `bun` refresh

The rebuild lives in a compiled artifact inside a **bun global install**,
not in this repository, and any `bun install`, package refresh, or
reinstall of `@tobilu/qmd` overwrites it. `tools/nlc_novmm.sh` rebuilds
and reinstalls the `linux-x64-cuda` copy and `status` reports which
backend is present — but read the section above first, because that is
probably not the file this host loads.

Two implementation notes, both load-bearing:

- **Build the same tag.** `b8390`, which is what qmd's
  `node-llama-cpp` 3.18.1 bundles. A mismatched backend risks an ABI
  break that is far harder to diagnose than the abort.
- **The build emits `libggml-base.so.0`** while the shipped backend is
  unversioned. The script satisfies this with a symlink rather than a
  second copy: glibc registers a library by its **SONAME**, so both
  names resolve to one loaded copy instead of two copies of the backend
  registry. Verified all 40 ggml/llama symbols the rebuilt backend
  imports resolve against the shipped same-commit `libggml-base.so`.

#### What is ruled out

- **VRAM pressure.** ~20 GB free throughout; peak 4775 MiB.
- **Concurrency between qmd processes.** `lies.qmd.lock` already
  serialises those, and the earlier 4 Hz sampler never saw more than
  one `qmd` subprocess alive.
- **Concurrency *within* one qmd process.** qmd creates up to 8
  embedding contexts, computed from VRAM, each doing its own
  reservation — which looked like the obvious culprit and is **not**.
  Forcing `QMD_EMBED_PARALLELISM=1` was measured at **1 abort with,
  1 without**, neither library patched: the failure is one
  reservation's *size*, not a race between them.
- **Shadowing the CUDA library via `LD_LIBRARY_PATH`.** This produces a
  *false all-clear*, and it is the subtlest failure here. Pointing
  `LD_LIBRARY_PATH` at a rebuilt ggml set makes qmd report **zero CUDA
  aborts across three clean-looking runs** — because **all eight tests
  ERROR** (`EEEEEEEE`) and no embed ever runs. The surface symptom is
  `qmd embed failed` with *empty* stderr, so the run reads as a pass.
  Prepending a directory shadows **every** ggml library inside it, and
  node-llama-cpp's set is built for a single commit; swapping in another
  is an ABI mismatch. **Read the per-test outcome, never the abort
  count alone** — an abort count of zero is also what "nothing ran"
  looks like.
- **A retry.** Implemented, measured, and reverted (`05ec942`,
  `49196fe`, `dda200e`, reverted in `cbba1b7`):

  | variant                   | CUDA aborts | timeout/wedge errors |
  |---------------------------|-------------|----------------------|
  | baseline (3 runs)         | 3, 1, 1     | 0                    |
  | 2 retries + 2s/4s backoff | 0           | 4                    |
  | 1 retry, no backoff       | 0           | 5                    |

  The retry absorbs the abort and the extra embed is paid by the next
  query in the same run: `QmdWedgeError: qmd stopped emitting for 30s`
  plus `QmdTimeoutError: qmd query timed out after 60s`, with
  `last output: 'Embedding 3 queries...'`. Fifteen timeout/wedge
  occurrences against three CUDA aborts — a net loss on a shared
  machine. That behaviour (fix this, break that) is what an external
  flaky failure looks like, and it is *why the reservation size, not
  the concurrency, was the thing to check.*

#### Reproducing the abort at all

It is intermittent and does not reproduce in a simple embed loop: 25
sequential `qmd embed` calls over one reused index produced **0 aborts
with the stock backend**. It has only been observed through
`tests/integration/test_tag_filter_end_to_end.py`, which differs in
building a **fresh per-test index** for each of 8 tests while a qmd
daemon holds its own context. A repro harness that does not have those
properties will report a false all-clear — that is how the 25-embed
loop nearly produced a confident false negative.

### `INTEGRATION=1 pytest` locally is a different suite than plain `pytest`

`tests/integration/` is gated on `INTEGRATION=1`, so a plain `pytest`
run reports the whole file as skipped and exits 0. That green is not
evidence about the integration suite. Two things were found only by
running it locally, and CI could not have found either — CI has no qmd
daemon and skips the daemon-dependent tests:

- `test_release.py::test_the_declared_version_has_a_section` enforces
  that `pyproject.toml`'s version has a dated CHANGELOG section or is
  named in the `[Unreleased]` body. A version bump with three
  well-formed `[Unreleased]` entries and no version mentioned anywhere
  fails it.
- `test_search_daemon.py` (fixed in 0.48.3) patched one of the two
  registry accessors `_resolve_tag_collections` reads, so the retriever
  saw an empty collection set and `search` reported the whole tag
  expression as unknown — an assertion that read like a resolver bug.

**Run `INTEGRATION=1` before declaring a change verified locally.**
What survives that: `tests/integration/test_tag_filter_end_to_end.py`
fails intermittently on this host, with **a different subset on each
run** (observed: 3, then 5, then 2 failures, no two runs agreeing). The
varying subset is the diagnosis — a logic bug fails the same tests every
time.

Two distinct causes share this file, and they were long conflated:

- **`QmdTimeoutError: qmd query timed out after 60s`** — the contention
  stall recorded in the CUDA section above, reaching the retriever
  through the per-test throwaway index these tests seed.
- **A CUDA hard-abort during the fixture's `qmd_embed`** — see
  `qmd embed aborts on the CUDA VMM reservation` above for the real
  cause and its fix. This one surfaces as a setup ERROR with a
  `cuMemAddressReserve` backtrace, not a timeout, and a `bun install`
  will bring it straight back.

### `store_collections` can be emptied by qmd itself, and it does not announce it

Observed on this host 2026-10-04/05: `lies qmd status` reported
`collections: 0` with `document_drift` on all 117 collections, for a
corpus that was intact (6131 documents, 6131 content, 0 orphans).

**The cause is qmd's, not LIES'.** `syncConfigToDb`
(`@tobilu/qmd/dist/store.js:887`) upserts the external config's
collections and then **deletes every `store_collections` row the config
does not name**:

```js
const configNames = new Set(Object.keys(config.collections));
for (const [name, coll] of Object.entries(config.collections)) upsertStoreCollection(db, name, coll);
const dbCollections = db.prepare(`SELECT name FROM store_collections`).all();
for (const row of dbCollections) {
    if (!configNames.has(row.name)) db.prepare(`DELETE FROM store_collections WHERE name = ?`).run(row.name);
}
```

It is guarded only by `store_config.config_hash`, and it early-returns
while that matches. So a config at `~/.config/qmd/index.yml` that
momentarily declares **zero** collections empties the table, and the
hash written *for that empty config* then matches — making the wipe
self-perpetuating until the config changes again. `getStore()` runs the
sync on every qmd CLI store open (`dist/cli/qmd.js:26-40`), and
`resyncConfig()` clears the hash to force it.

**Why retrieval stays healthy through it**, which is what makes it hard
to spot: the daemon serves reads without passing through that sync, and
`validate_scope` reads the daemon's own `status` tool rather than
`store_collections`. Every query path saw a healthy index.

**What repairs it.** A later qmd store open with a mismatching hash
re-syncs every collection from `index.yml`. That is what happened here:
`store_collections` read 0 and then read 117 with no write in between.
Had the config *not* been restored, nothing would have — so check the
`collections:` block in `~/.config/qmd/index.yml` first, because
re-syncing an empty config is precisely what emptied the table.

`lies qmd status` reports this as one `registry_divergence` finding
carrying the cause and the remedy, rather than leaving it as N
identical drift entries. Read that field first when `collections` is 0
and `document_drift` is large; the two numbers being wildly different is
the signature.

### Live-index residue
Four orphan `content_vectors` rows and five `documents` rows for
`wiki_tag-filter-lib`, a collection absent from `store_collections`.

**The cause is a defect that no longer exists.** The five rows were
the tag-filter fixture's own page set — `index.md` plus the four
`FIXTURE_COLLECTIONS` — for a collection that was never registered in
`store_collections`. `tests/integration/test_tag_filter_end_to_end.py`
registers a collection and then calls `qmd_embed`; the embed hit the
CUDA reservation flake above, the exception propagated out of seeding
before the fixture's `yield`, and the fixture's teardown was wrapped
around the `yield` — so it never ran. Observed leaking at 2026-10-03
22:46, thirteen minutes after a run with three such aborts:
`leaked collections --- wiki_tag-filter-lib` in the live index. Two
earlier investigations had searched session logs for an unexplained
write; the write was this fixture, failing.

The rows themselves are stamped 07:42–08:15Z that morning, from a
run not recorded in any session transcript, and the four orphan
vectors *predate* the five documents — which one clean seed cannot
produce. So the same defect, run more than once, and the specific
rows are not attributable to the 22:46 run. The defect is the finding;
the exact run is not, and 0.47.1's claim does not rest on it.

**Cleaned 2026-10-04, operator action.** The daemon was stopped, the
residue removed in two transactions, and the index verified: 15
collections, **5987 documents = 5987 content = 5987 FTS**, 48984
vectors, `integrity_check` ok, `foreign_key_check` clean, and all
three residue classes zero through `lies qmd status` itself. The
corpus is back to 5987 — the count these docs carried before the
leak. A verified backup precedes the write at
`~/qmd-index-backup-20261004.sqlite`.

Removing the documents left five `content` rows with vectors and no
document, and removing those cascaded four vectors away. Both steps
were needed to land on 1:1:1; a cleanup that stops at the documents
trades one residue class for another. `index_orphans` checks
vectors-against-content and would not have seen the documentless
content, so the final verification checks all three directions
explicitly.

Fixed in 0.47.1: the cleanup moved into `_seeded_qmd_context`, which
wraps the *seeding*, and a cleanup that fails now rides along as a
note on the in-flight exception instead of replacing it.
`lies.qmd.integrity` reports all three classes (`collection_drift`,
`document_drift`, `index_orphans`) and the tag-filter session guard
fails loudly on recurrence.

### A stopped qmd daemon is diagnosable only if the log survives

qmd truncates `mcp.log` on every start, so the artefact that could
explain an unexpected daemon death is destroyed by the recovery
attempt — and LIES recycles the daemon routinely. On 2026-10-03 the
machine-global daemon stopped between 23:02Z and 06:35Z with no
answerable cause: no OOM record, the staleness marker older than the
pidfile, the CUDA abort followed by a verified-live daemon, no WSL
restart, and both integration runs clean.

`_down` now copies the log to `mcp.log.<stamp>` before stopping,
keeping `LOG_GENERATIONS_KEPT` (5). If a daemon dies again, look in
`~/.cache/qmd/` for the preserved generations first — and add a
generation here, because the next death should be answerable.

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

**The re-measure takes the minimum of up to `_ISOLATION_REPEATS` (3)
passes, not one.** One draw from a distribution this noisy is not a
measurement. Measured on this host, the same test on the same commit came
back 0.143s from one pass and 0.016s from twelve more, with the 0.15s
line between the two — a test costing 16ms, passing by 7ms. The question
the re-measure answers is what the test costs when nothing else competes,
and that is a floor over repeated observations rather than a draw. The
direction is sound: a test whose *fastest* pass is over the limit still
fails. The loop stops as soon as a pass clears, so the common run spawns
exactly one process.

## References

- Project overview: README.md
- Makefile targets: Makefile
- Release notes: CHANGELOG.md
