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
│   └── catalog.py   # `lies catalog` group: status/dump/reconcile/rebuild/render
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
├── mcp/             # FastMCP server (src/lies/mcp/server.py) — thin adapter
│                    # around WikiMemoryService; tools: init_wiki, query,
│                    # answer, lint, ground, wiki_search, wiki_read,
│                    # wiki_changes, file_knowledge, reindex; resources
│                    # include wiki://catalog and wiki://catalog/{slug}
│   ├── grounding.py # F19 grounding archivist (CitationSnippet + ArchivistDigest
│   │                # + truncate_at_word_boundary + pick_first_prose_span + ground())
│   └── daemon.py    # pidfile lifecycle for `lies mcp up/down/status`
├── memory/          # invisible-memory layer (see below)
│   ├── catalog.py   # sqlite wiki catalog: schema + CRUD + rebuild_from_disk
│   │                # + reconcile + render_markdown. Mirrors the design
│   │                # described in superpowers/specs/2026-09-04-f4b-f16-catalog-port-design.md
│   │                # Lives at <wiki_dir>/.lies/catalog.db
│   └── catalog_models.py  # CatalogPage (frozen BaseModel) + PageSection enum
├── orchestrator.py  # top-level Orchestrator; owns cross-cutting capabilities
├── qmd/             # qmd CLI + MCP adapters
│   └── daemon.py    # ensure/inspect qmd's own daemon (never stops it)
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
`~/.local/share/lies/<name>/`. The MCP `query` / `answer` / `cite` /
`ground` tools fan out across the global library at
`~/.local/share/lies/library/collections/<name>/` instead.

When an agent is asked to "look at a lies collection," treat it as a
reference to a library collection, not a wiki. The library is the
source of truth for retrieval in this environment.

Wiki code paths (`WikiMemoryService`, `wiki_search`, `wiki_read`,
`init_wiki`, `file_knowledge`, `WikiIdentity`, `MemoryPlan`,
page-author agents, `wiki://catalog` resource) remain in source for
future use. They are dormant — no wiki XDG instance currently exists
for them to point at. `lies init <name>` will create a new wiki if
invoked; that's expected for future wiki-mode users.

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
- The Pydantic AI main agent reads through `wiki_search` and `wiki_read`
  tools; the FastMCP server exposes the same tools plus an expanded
  `query` response (`citations`, `pages_read`, `changed_pages`).
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

**Render marker:** when the LLM renders the archivist's digest as
citation lines, wiki-only hits (where `source_kind="wiki"`) are
prefixed with `[secondary] ` to flag that the snippet is not
grounded in a primary source. Library hits render unprefixed.

```
[[mermaid/syntax/flowchart]] (Flowchart syntax): "flowchart TD; A-->B"     # library (primary)
[secondary] [[default/concepts/pydantic]] (Pydantic concept): "..."      # wiki-only (secondary)
```

## Quality gates

`make check` runs `lint + typecheck + format`. `make test` runs the full
pytest suite. Pre-commit hooks wrap the same targets so a commit that
lands in the repo has already passed all gates.

## References

- Project overview: README.md
- Makefile targets: Makefile
- Release notes: CHANGELOG.md
