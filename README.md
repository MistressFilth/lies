# LIES

[![CI](https://github.com/MistressFilth/lies/actions/workflows/ci.yml/badge.svg)](https://github.com/MistressFilth/lies/actions/workflows/ci.yml)

**Library of Inconsistent Explanations & Sources**

A Karpathy-pattern LLM wiki — a `pydantic-ai`-harness agent that maintains a
git-backed wiki of interlinked markdown files over a corpus of raw sources. The
schema (a per-wiki markdown file) defines page types, conventions, and
workflows. The human curates sources and asks questions; the agent does all
bookkeeping.

## Status

MCP server, invisible wiki memory, collection sync, source builders, and safe
lint repair are available on `main`. Claude Code and other MCP hosts can use
the stdio server documented below.

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

The stdio form above spawns one server per host session and is the right
default for single-session use.

To keep a warm server running independently of any host, run it as a
daemon:

```bash
lies mcp up                      # detached; prints the URL
claude mcp add --transport http lies http://127.0.0.1:8737/mcp
lies mcp status                  # pid, URL, uptime, log path
lies mcp down                    # stop it
```

`lies mcp up` binds `127.0.0.1:8737` by default. Pass `--port` to
run a daemon for a second wiki; `--host` may select another loopback
address such as `localhost`, `::1`, or any address in `127.0.0.0/8`.
The daemon is per-wiki — its pidfile lives at `<wiki>/.lies/mcp.pid` and
its output goes to `<wiki>/.lies/mcp.log`.

`lies mcp down` only stops daemons that `up` recorded. Servers a host
spawned on stdio are left alone, so stopping the daemon never kills a
live Claude Code session.

`lies mcp start` runs the stdio server in the foreground — the explicit
spelling of bare `lies mcp`, which is unchanged.

`lies mcp up` also ensures qmd's own MCP daemon is running, and the
agent's search routes through it. qmd is optional: if it is not
installed or fails to start, the LIES daemon still comes up with a
warning, and search runs degraded. Pass `--no-qmd` to skip the step.

When the daemon is unreachable mid-session, `QmdCapability` falls back to
an in-process FastMCP server that uses the same `wiki/index.md` path the
host `query` tool already used, prints a single stderr warning naming the
URL and the fix (`LIES_QMD_URL` or `qmd mcp --http --daemon`), and tags
every search result with `degraded: True` so the model knows the search
was not qmd-backed. Set `LIES_QMD_TRANSPORT=stdio` to opt back into a
`qmd mcp` subprocess per agent.

`lies mcp down` never stops qmd. That daemon is machine-global — one
fixed port, one index shared across every wiki and any other tool using
it — so stopping it would break sessions LIES knows nothing about. Use
`qmd mcp stop` yourself if you really want it down.

The daemon has no authentication, so `up` and the internal `_serve`
command refuse non-loopback bind hosts. Put an authenticated reverse
proxy in front if remote access is required.

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

## Source collection builders

LIES can ingest PDF, Sphinx, HTML, and bespoke source corpora. Three
formats are first-class; a fourth (`liquid`) is reserved for future
work and currently quarantines per-doc.

Add a collection by hand or use the LLM-driven author:

```bash
# Hand-written YAML.
$EDITOR .lies/collections/htmx.yaml
uv run lies sync htmx

# LLM-driven: the agent asks one question at a time.
uv run lies collections new htmx \
    --source https://github.com/bigskysoftware/htmx/tree/master/www/content \
    --prompt "the curated htmx docs at www/content, drop _templates and examples"
# Review the YAML on stdout, then re-run with --apply to write it.
uv run lies collections new htmx --source <url> --prompt "..." --apply
uv run lies sync htmx
```

For htmx-class messy corpora, the agent may also propose a
`scraper_cmd` pointing at a Python module outside the repo that
implements `BaseScraper`. Drop the module anywhere on `PYTHONPATH`
and reference it as `module:attr` or `path.py:attr`.

### Liquid sources

`source_format=liquid` enables per-file Liquid template conversion.
LIES reads `source.liquid`, optionally renders it via a Python callable
referenced from `Collection.config["render_cmd"]`, then converts the
HTML to Markdown via pandoc.

```yaml
# <wiki>/.lies/collections/liquid-theme.yaml
name: liquid-theme
source: ./raw/themes/dawn
source_format: liquid
config:
  render_cmd: my_package.shopify_render:render
  context:
    shop:
      name: "Test Shop"
```

The `render_cmd` follows the same `module:attr` import path as
`scraper_cmd`. `importlib` loads it at sync time. The callable signature
is `(template_bytes: bytes, context: dict) -> bytes` (returns HTML).

If `render_cmd` is omitted, the source file is passed through to
pandoc unchanged. Liquid tags are preserved as HTML in the rendered
markdown. This is the zero-config path for collections that already
deliver pre-rendered HTML.

After the first successful sync, the collection's
`WikiCollectionRef` is registered with `WikiMemoryService` for this
wiki root. Inspect with:

```bash
uv run lies collections show htmx
# name=htmx source=https://... tags=['docs']
# status: registered
```

Re-runs are idempotent. The registry is in-memory only; restart
loses it; the next `sync` re-registers.

## Configuration

Environment variables:

- `LIES_MODEL` — model identifier (default: `anthropic:claude-opus-4-7`)
- `LIES_WIKI_ROOT` — wiki root path (default: cwd)
- `LIES_LOG_LEVEL` — stdlib log level when logfire is inactive (default: `INFO`)
- `LOGFIRE_TOKEN` — if set, logfire is configured for observability and
  `pydantic-ai` is instrumented
- `LIES_QMD_TRANSPORT` — how the agent reaches qmd: `http` (default, uses
  the qmd daemon) or `stdio` (spawns a `qmd` process per agent)
- `LIES_QMD_URL` — qmd daemon URL (default: `http://127.0.0.1:8181`)

Most commands accept `--wiki-root` / `-w` to override the wiki root for one
invocation. `lies config` prints the active model and wiki root.

## Development

```bash
make init
make check
make test
```

## Architecture

A top-level `Orchestrator` (`src/lies/orchestrator.py`) dispatches user commands
to five sub-agents via harness's `SubAgents` and `DynamicWorkflow` capabilities:

A `FastMCP` server (`src/lies/mcp/server.py`) exposes the orchestrator's
operations to MCP-capable hosts (Claude Code, Cursor, etc.) over stdio.
See "Using LIES from Claude Code" above for the registration command.

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
- `lies ingest <collection>` — sync an existing collection (first-time
  LLM scraper generation is deferred).
- `lies ingest-source <source>` — ingest a local source path. The legacy
  source-path CLI surface; delegates to `Orchestrator.run_ingest` for
  host-side atomicity and rollback.
- `lies query <question>` — ask a question of the wiki.
- `lies lint [--fix]` — health-check the wiki (`--fix` applies the repair plan for safe_to_fix findings). Findings span six categories; LLM-backed categories are skipped with a `Sources` line when no model key is configured.
- `lies mcp` / `lies mcp start` — run the MCP server on stdio.
- `lies mcp up` / `down` / `status` — manage the detached http MCP daemon.
- `lies status` — show qmd status and the last few log entries.
- `lies config` — print the active model and wiki root.
- `lies version` — print the LIES version.
- `lies` (no subcommand) — enter the REPL (`/ingest`, `/query`, `/lint`,
  `/status`, `/commit`, `/exit`).

## Invisible memory

LIES reads and writes the wiki invisibly during normal interaction:

- The Pydantic AI main agent searches and reads relevant wiki pages
  through `wiki_search` and `wiki_read` tools.
- After the answer, a `MemoryEnricher` sub-agent proposes a
  structured `MemoryPlan` only when evidence warrants it.
- The host validates the plan and applies it through
  `WikiMemoryService`, which writes the page, rebuilds the index,
  appends the log, commits atomically, and refreshes the qmd
  derived index.
- Material changes surface in a small receipt at the end of the
  turn. Routine reads and bookkeeping stay out of the response.

Memory captures durable project knowledge (facts, source claims,
concepts, contradictions, crosslinks). It never captures user
preferences, working decisions, or task history. Source files in
`raw/` are immutable.

- Transient persistence failures (`WikiLockBusy`, `WikiWriteConflict`, `WikiCommitFailed`) replay automatically on the next turn; receipt surfaces `(memory: queued for retry — <reason>)` immediately and `(memory: deferred after 3 attempts — <reason>)` if the cap is hit.

## Parsing and Ingestion

LIES includes a state-machine ETL pipeline for ingesting documentation
sources into the wiki. The pipeline is independent of `WikiMemoryService`
(bulk writes go through `atomic_commit` directly) and runs as four
stages:

1. **SCRAPE** — fetch + parse + manifest emit.
2. **NORMALIZE** — format dispatch + Obsidian convention apply.
3. **WRITE** — hash compare + atomic_commit (skips unchanged docs).
4. **QMD_UPDATE** — incremental qmd update per collection.

Commands:

- `lies sync <collection>` — re-ingest changed docs only (`--force` for full).
- `lies ingest <collection>` — bootstrap a collection (existing or new).
- `lies reindex` — reconcile / embed / cleanup flags.
- `lies collections list|show|modify` — manage collection configs.

See `docs/superpowers/plans/2026-08-01-parsing-and-ingestion-plan.md`
for the implementation plan and `2026-08-01-parsing-and-ingestion-design.md`
for the design spec.

## License

[MIT](LICENSE).

## Project links

- [Changelog](CHANGELOG.md)
- [Agent instructions](AGENTS.md)
- [License](LICENSE)
