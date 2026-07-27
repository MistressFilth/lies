# LIES — Design Spec

**Date:** 2026-07-27
**Status:** Approved for planning
**Repo:** https://github.com/MistressFilth/lies

## Context

LIES is a faithful implementation of Andrej Karpathy's [LLM Wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) pattern, applied to software engineering. The acronym ("Library of Inconsistent Explanations & Sources") is Discordian flavor — the design makes no special architectural concessions to the name beyond what Karpathy's pattern already does.

The pattern: a pydantic-ai agent maintains a git-backed wiki of interlinked markdown files over a corpus of raw sources. The schema (a per-wiki markdown file) defines page types, conventions, and workflows. The human curates sources and asks questions; the agent does all bookkeeping.

## Goals

- Faithful Karpathy implementation: raw / wiki / schema layers; ingest / query / lint operations; `index.md` and `log.md`; wiki-as-git.
- Parametric over source type (external research, internal codebase, personal notebook) per Karpathy's "everything is modular" framing.
- Parallel ingest via `DynamicWorkflow` to handle the "10–15 page cross-reference fan-out" Karpathy describes.
- Cross-session continuity via harness `Memory`.
- Local-first search via `qmd` (hybrid BM25 + vector + rerank).
- CLI + REPL interface for single-user use.

## Non-Goals

- Multi-user / team features (no auth, no conflict resolution).
- Web UI.
- Embedding-based RAG (the wiki itself is the search corpus; `qmd` handles hybrid search).
- Graph database backend.
- A LIES-specific "inconsistency page type" or "adjudicate operation" — Karpathy's `lint` already covers contradictions; the LIES name is flavor, not architecture.
- Source reliability scoring.
- Multimodal sources (images, audio) — deferred; Karpathy notes LLMs handle text-then-images poorly anyway.

## Stack

- **Language:** Python 3.10+
- **Package manager:** `uv`
- **Agent framework:** `pydantic-ai` (slim)
- **Capability library:** `pydantic-ai-harness` (CodeMode, Memory, Planning, Sub-agents, DynamicWorkflow, file system, shell)
- **Search:** `qmd` (MCP server primary; CLI shell-out for batch ops)
- **CLI:** Typer
- **Model:** `anthropic:claude-opus-4-7` default, env-overridable via `LIES_MODEL`
- **Version control:** the wiki itself is a git repo; LIES tool source lives in its own repo

## Architecture

```
┌─────────────────────────────────────────────────┐
│  CLI (Typer)  ───────►  Orchestrator Agent     │
│  REPL                     │                    │
└───────────────────────────┼────────────────────┘
                            │
              ┌─────────────┴─────────────┐
              ▼                           ▼
        DynamicWorkflow              Memory
              │
   ┌──────────┼──────────┬──────────┬──────────┐
   ▼          ▼          ▼          ▼          ▼
source-    page-      indexer    linter     query-
reader     writer                            synthesizer
   │          │          │          │          │
   └──────────┴──────────┴──────────┴──────────┘
              │
              ▼
         CodeMode (sandboxed)
              │
              ▼
   ┌──────────┬──────────┬──────────┐
   ▼          ▼          ▼          ▼
file       shell      qmd       WebSearch
system     (qmd CLI)  MCP       (Exa)
```

### Sub-agents

| Sub-agent | Responsibility | Inputs | Outputs |
|---|---|---|---|
| `source-reader` | Read and extract from a raw source | source path / URL | structured extraction (claims, entities, concepts) |
| `page-writer` | Create / update wiki pages per schema | extraction + target page list | file diffs |
| `indexer` | Maintain `index.md` and `log.md` | page write events | `index.md` and `log.md` diffs |
| `linter` | Health-check the wiki | wiki root | `lint-report.md` + optional fix diffs |
| `query-synthesizer` | Answer a user question | question + qmd results | answer text + cited page links |

Sub-agents are isolated; the orchestrator composes them. The orchestrator never directly reads or writes wiki files — it always goes through a sub-agent (or `CodeMode` for batch ops). This keeps file mutations auditable and schema-respecting.

### Harness capabilities

| Capability | Used for |
|---|---|
| `CodeMode` | Atomic multi-file writes during ingest; one `run_code` call per ingest step |
| `Memory` | Cross-session continuity: schema state, last-ingested source, open lint findings |
| `Planning` | Break "ingest touches 10–15 pages" into an ordered plan |
| `DynamicWorkflow` | Parallel cross-reference updates during ingest |
| File system | Read / write the wiki and raw sources |
| Shell | `qmd update`, `qmd status`, `git add`/`commit` |

### qmd integration

- **MCP server** (`qmd mcp` over stdio): primary tool surface. Tools `query`, `get`, `multi_get`, `status` exposed to the agent.
- **CLI shell-out**: `qmd update` after ingest, `qmd status` for diagnostics. The orchestrator shells out via harness `Shell` with an allowlist of qmd subcommands.
- If `qmd` is not installed, the agent falls back to `index.md`-only navigation (per Karpathy: works at moderate scale).

## Wiki layout

```
<wiki>/
├── raw/                    # immutable sources
│   ├── articles/
│   ├── code/
│   ├── notes/
│   └── assets/             # downloaded images (Karpathy: bind Obsidian hotkey)
├── wiki/                   # LLM-owned markdown
│   ├── index.md            # content-oriented catalog
│   ├── log.md              # chronological log
│   ├── overview.md         # synthesis
│   ├── lint-report.md      # last lint output
│   └── ...                 # entity / concept / comparison pages
├── .lies/
│   └── schema.md           # per-wiki schema override (optional)
└── .git/                   # wiki version history
```

The wiki root is the working tree of a git repo. Every ingest produces one commit; the commit message is the log entry.

## Schema

The schema is the LIES-specific configuration file (Karpathy's CLAUDE.md / AGENTS.md equivalent). It tells the agent:

- What page types exist (entity, concept, comparison, etc.)
- What frontmatter fields each page type carries
- The ingest workflow (what to extract, what pages to touch)
- The query workflow (search strategy, citation format)
- The lint workflow (what to check, what to flag)

### Loading

1. Look for `<wiki>/.lies/schema.md`
2. If absent, load `src/lies/schema/default_schema.md` (shipped with LIES)
3. If absent, error: LIES not properly installed

The schema is injected into the orchestrator's system prompt at agent construction. Sub-agents receive the relevant subset.

### Default schema (shipped)

Karpathy-faithful. Defines:

- Page types: `entity`, `concept`, `comparison`, `overview`, `source`
- Frontmatter: `title`, `type`, `tags`, `created`, `updated`, `sources` (list of source paths)
- Ingest workflow: extract claims → identify entities/concepts → identify comparisons → write pages → update index → append log
- Query workflow: search `index.md` → `qmd query` → read top-N pages → synthesize with citations
- Lint workflow: contradictions / stale claims / orphans / missing pages / missing cross-refs / data gaps

Users copy the default to `<wiki>/.lies/schema.md` and modify as their wiki matures — per Karpathy: "you and the LLM co-evolve [the schema] over time."

## CLI surface

```
lies init <path>                    Initialize a new wiki
lies ingest <source>                Ingest a source (path, URL, or - for stdin)
lies query <question>               Query the wiki
lies lint [--fix]                   Health check, optionally apply safe fixes
lies status                         qmd status + last 10 log entries
lies                                REPL: chat with the orchestrator
```

### REPL commands (in addition to free-form)

- `/ingest <source>` — shorthand for `lies ingest`
- `/query <question>` — shorthand for `lies query`
- `/lint` — shorthand for `lies lint`
- `/status` — shorthand for `lies status`
- `/commit` — force a git commit of pending wiki changes
- `/exit` — leave the REPL

## Operations

### Ingest

1. Orchestrator receives `ingest <source>`.
2. `source-reader` reads the source (markdown / plain text from disk; URL via harness `WebSearch` Exa tool; PDF converted to text first). Returns structured extraction: claims, entities, concepts, comparisons.
3. Orchestrator consults `Memory` for the current wiki state (existing entities, recent ingestions, open lint findings).
4. `Planning` decomposes the work into ordered page operations: which new pages, which updates to existing pages, in what order, with what cross-references.
5. `DynamicWorkflow` runs `page-writer` + `indexer` in parallel for each page operation. All writes go through `CodeMode` for atomicity per ingest step.
6. `indexer` updates `index.md` and appends to `log.md` atomically with the page writes. The log entry uses a parseable prefix (per Karpathy: `## [YYYY-MM-DD] ingest | Title`).
7. Orchestrator shells out to `qmd update` to reindex.
8. Orchestrator creates a git commit: message = log entry, body = list of touched files.
9. `Memory` is updated: last-ingested source, recent entities, recent changes.

If any step fails, the in-flight file changes are reverted (git stash or working-tree reset) and the error is surfaced to the user.

### Query

1. Orchestrator receives `query <question>`.
2. `query-synthesizer` calls `qmd query` (hybrid BM25 + vector + rerank) via MCP. Filters to top-N (default 5).
3. Reads each top page in full via `qmd get`.
4. Synthesizes an answer with inline citations (`[page-name](path)`).
5. The orchestrator offers to file the answer as a new wiki page (per Karpathy: "good answers can be filed back"). Default: ask once, remember the user's choice in `Memory`.

### Lint

1. Orchestrator receives `lint`.
2. `linter` walks the wiki:
   - **Contradictions** — pages that assert conflicting claims. Read every page, build a claim graph, surface conflicts.
   - **Stale claims** — pages citing a source where newer sources have superseded the claim.
   - **Orphans** — pages with no inbound links from `index.md` or other pages.
   - **Missing pages** — entities / concepts mentioned in pages but lacking their own page.
   - **Missing cross-references** — pages that should link to each other but don't.
   - **Data gaps** — questions a web search could answer; the linter suggests search queries.
3. Writes `wiki/lint-report.md` with prioritized findings.
4. If `--fix`, the linter applies safe fixes: add missing cross-refs, create stub pages for missing entities. Reports which fixes were applied and which require human review.
5. Each lint run appends a `## [YYYY-MM-DD] lint | N findings` entry to `log.md`.

## Error handling

| Failure | Behavior |
|---|---|
| Source path / URL unreadable | Surface to user, do not crash |
| Schema missing | Fall back to default, log warning |
| `qmd` not installed | Fall back to `index.md`-only navigation |
| `qmd query` returns no results | Fall back to `index.md` scan, surface "no results" to user |
| Git commit failure | Roll back in-flight wiki changes (working-tree reset), surface error |
| LLM API rate limit / error | Retry with exponential backoff (3 attempts), surface if persistent |
| Sub-agent timeout / runaway | Cap via `DynamicWorkflow(max_agent_calls=...)`; surface partial output |
| `CodeMode` sandbox error | Roll back the affected file writes, surface error |
| Lint `--fix` would create a non-trivial change | Skip, list in `lint-report.md` as "requires human review" |

## Testing

- **Unit** — each sub-agent with a mocked LLM. Verify it produces the right structure given a known input.
- **Integration** — fixture wiki at `tests/fixtures/sample-wiki/` (5 sources, ~20 pages). Run full ingest → query → lint round-trip; verify file diffs match snapshots.
- **Schema conformance** — every page in the fixture wiki validates against the default schema's frontmatter spec.
- **`index.md` / `log.md` snapshots** — golden-file tests; updates produce the expected diff.
- **End-to-end** — `lies init` → `lies ingest` (×3) → `lies query` → `lies lint --fix` on a fresh tmp dir. Verify the wiki is coherent and `qmd query` returns the ingested content.
- **CLI smoke tests** — every command runs against the fixture wiki; exit code, stdout, and stderr are snapshot-tested.

## Project structure

```
lies/
├── pyproject.toml                  # uv-managed
├── README.md
├── docs/
│   └── superpowers/
│       └── specs/
│           └── 2026-07-27-lies-design.md
├── src/lies/
│   ├── __init__.py
│   ├── cli.py                      # Typer app
│   ├── orchestrator.py             # top-level agent
│   ├── agents/
│   │   ├── __init__.py
│   │   ├── source_reader.py
│   │   ├── page_writer.py
│   │   ├── indexer.py
│   │   ├── linter.py
│   │   └── query_synthesizer.py
│   ├── capabilities/
│   │   ├── __init__.py
│   │   ├── code_mode.py            # CodeMode wiring
│   │   ├── memory.py               # Memory wiring
│   │   ├── planning.py             # Planning wiring
│   │   └── dynamic_workflow.py     # DynamicWorkflow wiring
│   ├── qmd/
│   │   ├── __init__.py
│   │   ├── mcp_client.py           # qmd MCP client
│   │   └── cli.py                  # qmd shell-out wrapper
│   ├── schema/
│   │   ├── default_schema.md
│   │   └── loader.py
│   ├── wiki/
│   │   ├── __init__.py
│   │   ├── layout.py               # wiki root conventions
│   │   └── git.py                  # commit helpers
│   └── utils/
│       ├── __init__.py
│       └── logging.py
├── tests/
│   ├── unit/
│   ├── integration/
│   └── fixtures/
│       └── sample-wiki/
└── .github/
    └── workflows/
        └── ci.yml
```

## Open questions

None. The model defaults to `anthropic:claude-opus-4-7` and the interaction model is Typer + REPL; both are env-overridable.

## References

- Karpathy, "LLM Wiki," https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f
- `pydantic-ai-harness`, https://github.com/pydantic/pydantic-ai-harness
- `qmd`, https://github.com/tobi/qmd
