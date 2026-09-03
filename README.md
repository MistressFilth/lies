# LIES

[![CI](https://github.com/MistressFilth/lies/actions/workflows/ci.yml/badge.svg)](https://github.com/MistressFilth/lies/actions/workflows/ci.yml)

**Library of Inconsistent Explanations & Sources**

Most LLM-document tools work like RAG: drop sources in, retrieve chunks at query time, regenerate an answer. Nothing accumulates. Ask a subtle question that needs five sources synthesized and the model pieces together the same fragments every time.

LIES is different. After every source you add and every question you ask, an agent reads what changed, extracts the key information, and quietly updates the wiki — entity pages, concept summaries, cross-references, contradictions. The wiki is a persistent, compounding artifact. You never (or rarely) write the wiki yourself; the agent maintains it.

Three layers: **raw/** (your curated sources, immutable), **wiki/** (the agent's markdown; LLM-owned), **schema.md** (the contract that tells the agent how to behave). You curate and ask; the agent does the bookkeeping.

## Invisible memory

LIES reads and writes the wiki invisibly during normal interaction:

- The Pydantic AI main agent searches and reads relevant wiki pages through `wiki_search` and `wiki_read` tools.
- After the answer, a `MemoryEnricher` sub-agent proposes a structured `MemoryPlan` only when evidence warrants it.
- The host validates the plan and applies it through `WikiMemoryService`, which writes the page, rebuilds the index, appends the log, commits atomically, and refreshes the qmd derived index.
- Each invisible write appends one line to `<wiki>/.lies/memory_plans.jsonl`. Inspect with `lies memory`; the MCP resource `wiki://memory-changes` exposes the same data; `lies memory reconcile` rebuilds from `git log` if the sidecar drifts.
- Material changes surface in a small receipt at the end of the turn. Routine reads and bookkeeping stay out of the response.

Memory captures durable project knowledge (facts, source claims, concepts, contradictions, crosslinks). It never captures user preferences, working decisions, or task history. Source files in `raw/` are immutable.

Transient persistence failures (`WikiLockBusy`, `WikiWriteConflict`, `WikiCommitFailed`) replay automatically on the next turn; receipt surfaces `(memory: queued for retry — <reason>)` immediately and `(memory: deferred after 3 attempts — <reason>)` if the cap is hit.

## Status

MCP server, invisible wiki memory, collection sync, source builders,
qmd auto-embed after sync, and safe lint repair are available on
`main`. Claude Code and other MCP hosts can use the stdio server
documented below.

## Quick start

```bash
uv sync
uv run lies ingest pydantic-ai --source https://pydantic.dev/docs/ai/llms.txt
uv run lies query "What do my sources say about X?"
uv run lies lint
```

The first `ingest` invocation bootstraps both the wiki (under
`$XDG_DATA_HOME/lies/pydantic-ai`) and the collection YAML (under
`$XDG_CONFIG_HOME/lies/pydantic-ai/collections/pydantic-ai.yaml`). Pass
`--wizard` to route the bootstrap through the interactive
`collection_author_agent` instead of the bare scaffold.

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
# Register for the current user, defaulting to wiki name "default":
claude mcp add --transport stdio lies -- uv run --project . lies mcp

# Pin a specific wiki by setting LIES_WIKI_NAME:
claude mcp add --transport stdio --env LIES_WIKI_NAME=my-research \
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
The daemon is per-wiki — its pidfile lives at
`$XDG_RUNTIME_DIR/lies/<name>/mcp.pid` and its output goes to
`$XDG_STATE_HOME/lies/<name>/mcp.log`.

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

- `init_wiki(name)` — bootstrap a new wiki by name (creates XDG role-routed dirs).
- `ingest_source(collection, name?, no_llm=False)` — atomic ingest.
  Default runs the LLM round-trip (`source_reader_agent` →
  `page_writer_agent` → `WikiMemoryService.apply_plan`); pass
  `no_llm=True` to demote to the legacy `sync_collection` shim for
  bulk-scrape semantics.
- `query(question, name?)` — synthesized answer (structured result
  with `fallback_used` and `fallback_reason`).
- `lint(name?)` — health-check the wiki.
- `migrate_xdg(legacy_path, name)` — one-shot bridge from legacy `<wiki>/.lies/` to XDG.

…and these resources:

- `wiki://status` — qmd status + last 10 log entries.
- `wiki://index`, `wiki://log`, `wiki://lint-report` — raw wiki artifacts.
- `wiki://page/{path}` — any page under `wiki/` (relative path; traversal
  rejected).

The server also exposes one prompt (`ask_wiki`) for asking the wiki.

Wiki selection: every tool accepts an optional `name` parameter.
Resolution chain: explicit `name` → `LIES_WIKI_NAME` env → `default`.
For multi-project workspaces, register one MCP server per wiki.

## Source collection builders

LIES can ingest PDF, Sphinx, HTML, Liquid, and bespoke source corpora.
All four named formats are first-class; bespoke dispatches user-provided
scrapers for other formats.

## Advanced

### Manual authoring (advanced)

The bootstrap path covers the common case (URL → bare YAML → sync). For
non-URL corpora or hand-tuned scrapers, write the YAML directly:

```bash
$EDITOR "$XDG_CONFIG_HOME/lies/$LIES_WIKI_NAME/collections/<name>.yaml"
uv run lies sync <name>
```

For bespoke scrapers outside the repo, set `scraper_cmd: module:attr`
referencing a `BaseScraper` subclass on `PYTHONPATH`.

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

- `LIES_WIKI_NAME` — wiki name (default: `default`); resolved under
  `$XDG_DATA_HOME/lies/<name>/`
- `LIES_LANG` — wiki language; resolves to `en` when unset. Read before any per-wiki `lies.toml`.
- `LIES_LOG_LEVEL` — stdlib log level when logfire is inactive (default: `INFO`)
- `LOGFIRE_TOKEN` — if set, logfire is configured for observability and
  `pydantic-ai` is instrumented
- `LIES_QMD_TRANSPORT` — how the agent reaches qmd: `http` (default, uses
  the qmd daemon) or `stdio` (spawns a `qmd` process per agent)
- `LIES_QMD_URL` — qmd daemon URL (default: `http://127.0.0.1:8181`)
- `LIES_XDG_DATA_HOME` — overrides `$XDG_DATA_HOME` for LIES
- `LIES_XDG_CONFIG_HOME` — overrides `$XDG_CONFIG_HOME` for LIES
- `LIES_XDG_RUNTIME_DIR` — overrides `$XDG_RUNTIME_DIR` for LIES
- `LIES_XDG_STATE_HOME` — overrides `$XDG_STATE_HOME` for LIES
- `LIES_XDG_CACHE_HOME` — overrides `$XDG_CACHE_HOME` for LIES
- `LIES_ORCHESTRATOR_MODEL`, `LIES_SOURCE_READER_MODEL`, `LIES_PAGE_WRITER_MODEL`, `LIES_INDEXER_MODEL`, `LIES_LINTER_MODEL`, `LIES_QUERY_SYNTHESIZER_MODEL`, `LIES_ENRICHER_MODEL`, `LIES_REPAIR_MODEL` — per-agent model override. Non-empty value beats `providers.toml`.

Most subcommands accept `--name` to override the wiki name for one
invocation. The bare `lies` REPL (no subcommand) reads the wiki name
from `$LIES_WIKI_NAME` only; set the env var to switch wikis in the REPL.
`lies config` prints the active model and wiki name.

### Bootstrapping providers

On a fresh install with no `providers.toml`, run the wizard:

```bash
uv run lies providers init
```

The wizard walks three steps — provider catalog → default model → per-agent assignment — and writes `<XDG_CONFIG_HOME>/lies/providers.toml` via atomic rename. At least one provider is required to write the file; the catalog step re-prompts on a blank-name exit with an empty catalog. Back-out is `^C` only. Subsequent edits:

```bash
uv run lies providers add <name> --type anthropic_compatible \
    --api-key-env MINIMAX_API_KEY \
    --base-url https://api.minimax.io/anthropic

uv run lies providers assign source_reader minimax:MiniMax-M3
uv run lies providers set-default anthropic:claude-opus-4-7
uv run lies providers unassign linter
uv run lies providers check
```

Companion commands refuse if the file is missing; the CLI suggests
`lies providers init` first.

### Provider and model configuration

LIES reads `$XDG_CONFIG_HOME/lies/providers.toml` at orchestrator construction. The file declares one or more providers and assigns a model to each agent:

```toml
[providers.anthropic]
type = "anthropic"
api_key_env = "ANTHROPIC_API_KEY"

[providers.minimax]
type = "anthropic_compatible"
base_url = "https://api.minimax.io/anthropic"
api_key_env = "MINIMAX_API_KEY"

default_model = "anthropic:claude-opus-4-7"

[agents]
orchestrator = "anthropic:claude-opus-4-7"
source_reader = "minimax:MiniMax-M3"
# ... one entry per agent in AGENT_ROSTER.
```

`type = "anthropic"` resolves through pydantic-ai's built-in provider. `type = "anthropic_compatible"` constructs an `AnthropicModel` directly with a custom `AsyncAnthropic(base_url=..., api_key=...)`.

`lies config` prints every agent and its resolved model. Missing `providers.toml` is non-fatal — every agent falls back to `default_model` and a warning names the expected path.

## Development

```bash
make init
make check
make test
```

## Architecture

The agent maintains the wiki invisibly during normal interaction. See [Invisible memory](#invisible-memory) for the contract.

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

The wiki is a git repository on disk, rooted under
`$XDG_DATA_HOME/lies/<name>/` by default:

```
$XDG_DATA_HOME/lies/<name>/     # wiki content root
├── raw/                        # immutable sources (the human curates these)
└── wiki/                       # LLM-owned markdown
    ├── index.md                # catalog of pages (wiki-wide)
    ├── log.md                  # append-only log (wiki-wide)
    ├── overview.md
    └── <collection>/           # per-collection subdir; qmd is registered here
        └── <page-type>/<name>.md

$XDG_CONFIG_HOME/lies/<name>/   # per-wiki configuration
└── schema.md                   # per-wiki schema override (optional)

$XDG_RUNTIME_DIR/lies/<name>/   # transient runtime state (locks, pidfile)

$XDG_STATE_HOME/lies/<name>/    # logs/scratch/poison
└── mcp.log

$XDG_CACHE_HOME/lies/<name>/    # hashes/manifests
```

CLI commands (`src/lies/cli/`):

- `lies init <name>` — initialize a new wiki by name (creates all five
  role-routed XDG directories, copies default schema, `git init`, initial commit).
- `lies migrate-xdg <legacy-path> --name <name>` — one-shot bridge from
  legacy `<path>/.lies/` to XDG role-routed directories.
- `lies ingest <collection> [--source URL] [--wizard]` — bootstrap (wiki + YAML if missing) and sync. `--wizard` routes the bootstrap through `collection_author_agent`.
- `lies sync [<collection>] [--source URL] [--wizard]` — sync one collection, or every collection in the wiki when no positional is given. Pass `--source` to bootstrap a missing YAML (single-collection mode only); `--wizard` routes the bootstrap through `collection_author_agent`.
- `lies ingest-source <source> --collection NAME [--wizard] [--no-llm]` — atomic single-source ingest; registers a collection YAML (required). Legacy source-only form is removed. Default path runs the LLM round-trip (`source_reader_agent` → `page_writer_agent` → `WikiMemoryService.apply_plan`); pass `--no-llm` to demote to the legacy `sync_collection` shim for bulk-scrape semantics. `--wizard` routes the bootstrap through `collection_author_agent`.
- `lies query <question>` — ask a question of the wiki; answers are
  LLM-synthesized with citations over qmd-retrieved pages, falling back
  to the previous extractive output when no model is available.
- `lies lint [--fix]` — health-check the wiki (`--fix` applies the repair plan for safe_to_fix findings). Findings span six categories; LLM-backed categories are skipped with a `Sources` line when no model key is configured.
- `lies mcp` / `lies mcp start` — run the MCP server on stdio.
- `lies mcp up` / `down` / `status` — manage the detached http MCP daemon.
- `lies status` — show qmd status, recent invisible writes, and the last
  few log entries (`--memory-limit N` to skip or limit the writes section).
- `lies memory [--limit|--pages|--ops|--since|--json]` — show recent
  MemoryPlan applications from the JSONL sidecar.
- `lies memory reconcile` — rebuild the sidecar from `git log --grep='^memory:'`.
- `lies memory truncate --keep N [--force]` — cap the sidecar to its
  last N rows.
- `lies config` — print the active model and wiki name.
- `lies version` — print the LIES version.
- `lies` (no subcommand) — enter the REPL (`/ingest`, `/query`, `/lint`,
  `/status`, `/commit`, `/exit`).

## Parsing and Ingestion

LIES includes a state-machine ETL pipeline for ingesting documentation
sources into the wiki. The pipeline is independent of `WikiMemoryService`
(bulk writes go through `atomic_commit` directly) and runs as four
stages:

1. **SCRAPE** — fetch + parse + manifest emit.
2. **NORMALIZE** — format dispatch + Obsidian convention apply.
3. **WRITE** — hash compare + atomic_commit (skips unchanged docs);
   one subdirectory per collection under the wiki root
   (`wiki/<collection>/<path>`), and a non-fatal post-commit hook
   re-registers the qmd collection against that subdir, refreshes
   the qmd index, and embeds the new chunks
   (`qmd embed -c <collection>`).
4. **QMD_UPDATE** — incremental qmd update per collection.

Per-collection subdirs are how LIES scopes qmd collections. Each
collection's qmd registration points at its own
`wiki/<collection>/` subdir, so `qmd query` and the MCP
`wiki_search` tool return only that collection's pages when the
caller scopes by collection. The same `wiki/index.md` is
rebuilt by `rebuild_index` after every successful sync.

Commands:

- `lies sync <collection>` — re-ingest changed docs only (`--force` for full).
- `lies ingest <collection>` — bootstrap a collection (existing or new).
- `lies reindex --reconcile` — sync each collection.
- `lies collections list|show|modify` — manage collection configs (modify writes immediately; see `--help`).

See `docs/superpowers/plans/2026-08-01-parsing-and-ingestion-plan.md`
for the implementation plan and `2026-08-01-parsing-and-ingestion-design.md`
for the design spec.

## License

[MIT](LICENSE).

## Project links

- [Changelog](CHANGELOG.md)
- [Agent instructions](AGENTS.md)
- [License](LICENSE)
