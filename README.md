# LIES

**Library of Inconsistent Explanations & Sources**

A Karpathy-pattern LLM wiki — a `pydantic-ai`-harness agent that maintains a
git-backed wiki of interlinked markdown files over a corpus of raw sources. The
schema (a per-wiki markdown file) defines page types, conventions, and
workflows. The human curates sources and asks questions; the agent does all
bookkeeping.

## Status

MVP complete. The branch `feat/lies-mvp` is ready to merge to `main`.

## Quick start

```bash
uv sync
uv run lies init ./my-wiki
uv run lies ingest ./my-wiki/raw/articles/some-article.md
uv run lies query "What do my sources say about X?"
uv run lies lint
```

Launch the REPL (no subcommand) for an interactive session:

```bash
uv run lies
lies> /help
```

## Using LIES from Claude Code

LIES ships an MCP server. Register it with Claude Code once and the
wiki becomes available as tools and resources in any Claude Code
session:

```bash
# Register for the current user, defaulting to the current directory as
# the wiki root:
claude mcp add --transport stdio lies -- uv run --project . lies mcp

# Pin a specific wiki by setting LIES_WIKI_ROOT:
claude mcp add --transport stdio --env LIES_WIKI_ROOT=/path/to/my-wiki \
    lies -- uv run --project /path/to/lies lies mcp
```

After registration, Claude Code sees these tools:

- `init_wiki(path)` — bootstrap a new wiki.
- `ingest_source(source, wiki_root?)` — atomic ingest.
- `query(question, wiki_root?)` — synthesized answer (structured result
  with `fallback_used` and `fallback_reason`).
- `lint(wiki_root?)` — health-check the wiki.

…and these resources:

- `wiki://status` — qmd status + last 10 log entries.
- `wiki://index`, `wiki://log`, `wiki://lint-report` — raw wiki artifacts.
- `wiki://page/{path}` — any page under `wiki/` (relative path; traversal
  rejected).

The server also exposes one prompt (`ask_wiki`) for asking the wiki.

Wiki selection: every tool accepts an optional `wiki_root` parameter.
Resolution chain: explicit `wiki_root` → `LIES_WIKI_ROOT` env → cwd.
For multi-project workspaces, register one MCP server per wiki.

## Configuration

Environment variables:

- `LIES_MODEL` — model identifier (default: `anthropic:claude-opus-4-7`)
- `LIES_WIKI_ROOT` — wiki root path (default: cwd)
- `LIES_LOG_LEVEL` — stdlib log level when logfire is inactive (default: `INFO`)
- `LOGFIRE_TOKEN` — if set, logfire is configured for observability and
  `pydantic-ai` is instrumented

Most commands accept `--wiki-root` / `-w` to override the wiki root for one
invocation. `lies config` prints the active model and wiki root.

## Development

```bash
uv sync --all-extras
uv run pytest -v
uv run ruff check src/lies tests
uv run mypy src/lies
```

## Architecture

A top-level `Orchestrator` (`src/lies/orchestrator.py`) dispatches user commands
to five sub-agents via harness's `SubAgents` and `DynamicWorkflow` capabilities:

A `FastMCP` server (`src/lies/mcp/server.py`) exposes the orchestrator's
operations to MCP-capable hosts (Claude Code, Cursor, etc.) over stdio.
See "Using LIES from Claude Code" below for the registration command.

- `source-reader` — read a raw source and return a structured extraction
  (claims, entities, concepts, comparisons, summary).
- `page-writer` — create or update wiki pages from extracted material; returns
  `PageDiff` operations; never touches `index.md` or `log.md`.
- `indexer` — maintain `wiki/index.md` (the catalog) and `wiki/log.md`
  (the append-only log) from a list of `PageDiff` operations.
- `linter` — walk the wiki and produce a structured `LintReport`
  (contradictions, stale, orphans, missing pages, missing xrefs, data gaps).
- `query-synthesizer` — synthesize a cited answer from qmd search results;
  surfaces disagreements and notes what the wiki does NOT know.

The orchestrator owns cross-cutting `pydantic-ai-harness` capabilities:

- `CodeMode` — atomic multi-file writes during ingest.
- `Memory` — cross-session continuity: schema state, last-ingested source, open
  lint findings.
- `Planning` — break "ingest touches 10–15 pages" into an ordered plan.
- `DynamicWorkflow` — parallel cross-reference updates during ingest.
- File system — read/write the wiki and raw sources, with traversal guards.
- Shell — `qmd` and `git` allowlist.

`qmd` provides hybrid search (BM25 + vector + rerank) via MCP (primary) and CLI
shell-out (for `qmd update` after ingest and `qmd status` for diagnostics).

The wiki is a git repository on disk:

```
my-wiki/
├── raw/                 # immutable sources (the human curates these)
├── wiki/                # LLM-owned markdown
│   ├── index.md         # catalog of pages
│   ├── log.md           # append-only log
│   ├── overview.md
│   └── <page-type>/<name>.md
└── .lies/
    └── schema.md        # per-wiki schema override (optional)
```

CLI commands (`src/lies/cli.py`):

- `lies init <path>` — initialize a new wiki (creates dirs, copies default
  schema, `git init`, initial commit).
- `lies ingest <source>` — ingest a local source path.
- `lies query <question>` — ask a question of the wiki.
- `lies lint [--fix]` — health-check the wiki; `--fix` applies safe fixes.
- `lies status` — show qmd status and the last few log entries.
- `lies config` — print the active model and wiki root.
- `lies version` — print the LIES version.
- `lies` (no subcommand) — enter the REPL (`/ingest`, `/query`, `/lint`,
  `/status`, `/commit`, `/exit`).

## License

MIT.
