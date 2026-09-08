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
uv run lies ingest --source https://pydantic.dev/docs/ai/llms.txt --collection pydantic-ai
uv run lies query "What do my sources say about X?"
uv run lies lint
```

The first `ingest --source` invocation writes a deterministic mirror
file under the library (`$XDG_DATA_HOME/lies/library/collections/<name>/`)
and registers it in the library catalog. Use `--batch <DIR>` to walk a
directory of sources into one collection, or `--exclude-stem` /
`--exclude-dir` to skip noise. The ingest path is purely deterministic
(no LLM call).

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

## Page authoring

`lies page write` writes one markdown page directly to the wiki through
`WikiMemoryService.apply_plan`. Use it to author concept / entity /
comparison / overview / source / synthesis pages from any context the
agent isn't already covering.

```bash
# Write a concept page (body from stdin)
echo "## Definition
A hook intercepts events at fixed points." | \
  lies page write --collection claude-code --type concept \
    --slug hooks --title "Hooks" --body-file -

# Overwrite an existing page
lies page write --collection claude-code --type concept \
  --slug hooks --title "Hooks" --body-file body.md --force
```

The MCP equivalent (`mcp__plugin_lies__file_knowledge`) elicits
overwrite/rename/cancel on slug collision via `ctx.elicit`.

## Advanced

### Manual authoring (advanced)

The bootstrap path covers the common case (URL → bare YAML → sync). For
non-URL corpora or hand-tuned scrapers, write the YAML directly:

```bash
$EDITOR "$XDG_CONFIG_HOME/lies/$LIES_WIKI_NAME/collections/<name>.yaml"
uv run lies sync <name>
```

For bespoke scrapers outside the repo, set `scraper_cmd: module:attr`
referencing a `BaseScraper` subclass on `PYTHONPATH`. The `lies sync`
pipeline honors `scraper_cmd` end-to-end: the `ScraperFetcher` loads
the bespoke scraper via the same `module:attr` / `path.py:attr`
resolver the wiki side uses. A bespoke-loader failure propagates —
the fetcher never silently falls back to `pick_scraper` on
prefix/suffix heuristics, so misconfigured scrapers surface
immediately rather than ingesting nothing.

### Liquid sources

`source_format=liquid` enables per-file Liquid template conversion.
LIES reads `source.liquid`, optionally renders it via a Python callable
referenced from `Collection.config["render_cmd"]`, then converts the
HTML to Markdown via pandoc. `lies sync` routes `liquid` through the
`REGISTRY`-registered `LiquidBuilder` rather than the wiki-side
`normalize.py` `UnknownFormatError("liquid parsing not yet supported")`
fallback, so a Liquid collection syncs end-to-end without bespoke
plumbing.

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
type = "openai_compatible"
base_url = "https://api.minimax.io/v1"
api_key_env = "MINIMAX_API_KEY"

default_model = "anthropic:claude-opus-4-7"

[agents]
orchestrator = "anthropic:claude-opus-4-7"
source_reader = "minimax:MiniMax-M3"
# ... one entry per agent in AGENT_ROSTER.
```

Three provider types are accepted:

- `type = "anthropic"` resolves through pydantic-ai's built-in provider.
- `type = "anthropic_compatible"` constructs an `AnthropicModel` directly with a custom `AsyncAnthropic(base_url=..., api_key=...)`. Requires `base_url`.
- `type = "openai_compatible"` constructs an `OpenAIChatModel` with a custom `AsyncOpenAI(base_url=..., api_key=...)`. Requires `base_url`. Use this for the `https://api.minimax.io/v1` endpoint; the corresponding source_reader / page_writer / etc. agents should be wrapped in `pydantic_ai.output.PromptedOutput` because `MiniMax-M3` ignores `tool_choice` and `response_format=json_schema` on both endpoints.

`lies config` prints every agent and its resolved model. Missing `providers.toml` is non-fatal — every agent falls back to `default_model` and a warning names the expected path.

## Development

```bash
make init
make check
make test
```

`make check` runs ruff, pydantic-guidance (PG + PYD flake8), ty, and
ruff format. Run the pydantic-guidance lint on its own with
`make lint-pydantic-guidance`; the same check fires on every commit via
`.pre-commit-config.yaml`.

## Architecture

The agent maintains the wiki invisibly during normal interaction. See [Invisible memory](#invisible-memory) for the contract.

A top-level `Orchestrator` (`src/lies/orchestrator.py`) dispatches user commands
to four sub-agents via harness's `SubAgents` and `DynamicWorkflow` capabilities:

A `FastMCP` server (`src/lies/mcp/server.py`) exposes the orchestrator's
operations to MCP-capable hosts (Claude Code, Cursor, etc.) over stdio.
See "Using LIES from Claude Code" above for the registration command.

- `source-reader` — read a raw source and return a structured extraction
  (claims, entities, concepts, comparisons, summary).
- `page-writer` — create or update wiki pages from extracted material; returns
  `PageDiff` operations; never touches `index.md` or `log.md`.
- `linter` — walk the wiki and produce a structured `LintReport`
  (contradictions, stale, orphans, missing pages, missing xrefs, data gaps).
- `query-synthesizer` — synthesize a cited answer from qmd search results;
  surfaces disagreements and notes what the wiki does NOT know.

`wiki/index.md` (the catalog) is now maintained deterministically by the
sqlite-backed catalog port (`.lies/catalog.db` in the wiki dir), not by a
sub-agent. See [Storage layout](#storage-layout).

The orchestrator owns cross-cutting `pydantic-ai-harness` capabilities:

- `CodeMode` — atomic multi-file writes during ingest.
- `Memory` — cross-session continuity: schema state, last-ingested source, open
  lint findings.
- `Planning` — break "ingest touches 10–15 pages" into an ordered plan.
- `DynamicWorkflow` — parallel cross-reference updates during ingest.
- File system — read/write the wiki and raw sources, with traversal guards.
- Shell — `qmd` and `git` allowlist.

`qmd` provides hybrid search (BM25 + vector + rerank) via MCP (primary) and CLI
shell-out (for `qmd status` diagnostics).

### Storage layout

The wiki is a git repository on disk, rooted under
`$XDG_DATA_HOME/lies/<name>/` by default:

```
$XDG_DATA_HOME/lies/<name>/     # wiki content root (the git repo)
├── .gitignore                  # seeded by `lies init`; ignores `.lies/`
├── .lies/                      # gitignored; wiki-root derived state
│   └── memory_plans.jsonl      # invisible-write receipt sidecar
├── raw/                        # immutable sources (the human curates these)
└── wiki/                       # LLM-owned markdown
    ├── .lies/                  # gitignored; wiki-dir derived state
    │   ├── catalog.db          # sqlite wiki catalog (source-of-truth)
    │   ├── catalog.db-wal      # write-ahead log
    │   └── catalog.db-shm      # shared memory file
    ├── index.md                # read-only title-only catalog derivative
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

#### Library

The library is the global corpus of deterministic source mirrors —
a sibling of every wiki root under `$XDG_DATA_HOME/lies/`. Every
ingest across every wiki writes here, so the same ingested source
can feed multiple wikis without a re-fetch:

```
$XDG_DATA_HOME/lies/library/        # global corpus (shared across wikis)
├── .lies/                          # gitignored; library-root derived state
│   ├── catalog.db                  # sqlite library catalog (source-of-truth)
│   ├── catalog.db-wal              # write-ahead log
│   └── catalog.db-shm              # shared memory file
├── log.md                          # append-only library log
├── poison/                         # quarantined failures
└── collections/                    # one subdir per collection
    └── <collection>/               # deterministic mirror + frontmatter
        ├── raw/                    # raw source bytes (optional)
        └── .lies/manifest.json     # per-collection manifest
```

The library catalog is a separate sqlite database from the wiki's
`.lies/catalog.db`: the wiki catalog indexes the agent's wiki pages;
the library catalog indexes the ingested source mirrors.
`section='library'` covers active mirrors; `section='library-migrated'`
quarantines anything `lies migrate ingest-to-library` could not move.
Library mirrors are immutable once written — re-ingest with `--force`
to overwrite.

The catalog is a small sqlite database at `.lies/catalog.db` **inside the
wiki dir** (WAL journal mode, `busy_timeout=5000`), accompanied by its
`-wal` and `-shm` siblings. Note that this is a different `.lies/` from
the wiki-root one holding `memory_plans.jsonl`. All three catalog files
are gitignored — `lies init` seeds a `.gitignore` whose `.lies/` rule
covers every `.lies/` directory in the repo. `wiki/index.md` is a
read-only markdown derivative of that database, rendered on demand by
`lies catalog render`.

The catalog is kept in lockstep with the wiki by per-operation upserts
inside `WikiMemoryService.apply_plan` and a bulk upsert in the etl WRITE
stage, so it never needs a rebuild during normal operation. Reconcile
after any out-of-band file edits: `lies catalog reconcile --dry-run` to
preview, then drop the flag to apply.

CLI commands (`src/lies/cli/`):

- `lies init <name>` — initialize a new wiki by name (creates all five
  role-routed XDG directories, copies default schema, `git init`, initial commit).
- `lies migrate-xdg <legacy-path> --name <name>` — one-shot bridge from
  legacy `<path>/.lies/` to XDG role-routed directories.
- `lies ingest --source <PATH|URL> [--collection NAME] [--slug <slug>] [--title <title>] [--force] [--dry-run] [--exclude-stem <name> ...] [--exclude-dir <name> ...]` — deterministic single-source ingest into the library. No LLM round-trip; the 5-step pipeline (fetch → ETL → filter → mirror → catalog) writes a deterministic frontmatter mirror and atomic-commits one catalog upsert. `--collection` defaults to `--slug-prefix` or `default`; `--force` overwrites an existing mirror; `--dry-run` prints the plan without writing.
- `lies ingest --batch <DIR> --slug-prefix <name> [--force] [--dry-run] [--exclude-stem <name> ...] [--exclude-dir <name> ...]` — directory walk into one collection. Same pipeline as `--source` but iterates every eligible file under `<DIR>`.
- `lies sync [<collection>] [--source URL] [--wizard]` — sync one collection into the library, or every collection in the wiki when no positional is given. Pass `--source` to bootstrap a missing YAML (single-collection mode only); `--wizard` routes the bootstrap through `collection_author_agent`. Honors `Collection.scraper_cmd` (bespoke scrapers via `module:attr` / `path.py:attr`) and routes REGISTRY-registered source formats (sphinx / liquid / bespoke) through their builders before falling back to `format_dispatch`. Exits non-zero when the batch reports any `errors`.
- `lies query <question> [--collection NAME] [--no-file] [--force-file]`
  — ask a question of the wiki; answers are LLM-synthesized with
  citations over qmd-retrieved pages, falling back to the previous
  extractive output when no model is available. When the synthesizer
  marks an answer `should_file`, the answer is durably filed under
  `wiki/<collection>/synthesis/<file>`; `--collection NAME` selects
  the target subdir (required to write), `--no-file` skips the loop,
  `--force-file` writes regardless of the agent's verdict. Success
  prints a `(synthesis: durably filed - <op>: <path>)` receipt;
  failure prints `(synthesis: error — <reason>)`.
- `lies page write` — write one page directly to the wiki (F39).
- `lies lint [--fix]` — health-check the wiki (`--fix` applies the repair plan for safe_to_fix findings). Findings span six categories; LLM-backed categories are skipped with a `Sources` line when no model key is configured.
- `lies mcp` / `lies mcp start` — run the MCP server on stdio.
- `lies mcp up` / `down` / `status` — manage the detached http MCP daemon.
- `lies status` — show the library catalog count + migrated tally, qmd
  status, wiki catalog size, recent invisible writes, and the last few
  log entries (`--memory-limit N` to skip or limit the writes section).
- `lies catalog {status, dump, reconcile, rebuild, render}` — manage the
  sqlite wiki catalog (`.lies/catalog.db` inside the wiki dir). `status`
  prints the row count and schema version; `dump` lists rows (`--json`,
  `--source-pkg`, `--type` filters); `reconcile` syncs the catalog with
  files on disk (`--dry-run` to preview); `rebuild` forces a full
  backfill from disk; `render` writes the title-only `wiki/index.md`
  markdown derivative (`--out FILE`, default stdout). Mirrors the design
  described in superpowers/specs/2026-09-04-f4b-f16-catalog-port-design.md.
  See
  [Storage layout](#storage-layout).
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

LIES ingests documentation sources through a deterministic 5-step
pipeline. The pipeline is purely mechanical — no LLM call on the
ingest path:

1. **FETCH** — pull bytes from a URL/path via `ScraperFetcher`
   (honors `Collection.scraper_cmd` for bespoke loaders;
   routes REGISTRY-registered source formats — sphinx, liquid,
   bespoke — through their builders before falling back to
   `format_dispatch`).
2. **ETL** — format dispatch (HTML, PDF, Sphinx, Liquid, etc.) →
   per-collection builders → normalized markdown.
3. **FILTER** — drop docs excluded by `--exclude-stem` /
   `--exclude-dir`; reject empty bodies.
4. **MIRROR** — write one deterministic frontmatter mirror per
   doc at `<library>/collections/<collection>/<slug>.md` (or
   reject on slug collision unless `--force`).
5. **CATALOG + COMMIT** — upsert one row per mirror into
   `<library>/.lies/catalog.db` (sqlite WAL,
   `busy_timeout=5000`) inside the `LibraryWriter` atomic-commit
   envelope, then refresh qmd against the library path via a
   non-fatal post-commit hook.

Ingested sources live in the **library**
(`$XDG_DATA_HOME/lies/library/collections/<collection>/`), not
under `wiki/`. The library is the deterministic, immutable mirror
of curated sources; the wiki is the agent's downstream markdown
(F39 page-author + LLM-driven `MemoryEnricher` / F3 file-back).
See [Storage layout](#storage-layout) for the full directory
boundary.

The wiki catalog (`wiki/.lies/catalog.db`) is unchanged — it still
indexes the agent's wiki pages, not the library mirrors.
`wiki/index.md` is a read-only title-only derivative of that
catalog, emitted on demand by `lies catalog render`.

Commands:

- `lies ingest --source <PATH|URL> [--collection NAME] [--slug <slug>] [--title <title>] [--force] [--dry-run] [--exclude-stem ...] [--exclude-dir ...]` — deterministic single-source ingest into the library. No LLM round-trip; the 5-step pipeline writes a deterministic frontmatter mirror and atomic-commits one catalog upsert. `--collection` defaults to `--slug-prefix` or `default`; `--force` overwrites an existing mirror; `--dry-run` prints the plan without writing.
- `lies ingest --batch <DIR> --slug-prefix <name> [--force] [--dry-run] [--exclude-stem ...] [--exclude-dir ...]` — directory walk into one collection. Same pipeline as `--source` but iterates every eligible file under `<DIR>`.
- `lies sync <collection> [--source URL] [--wizard]` — sync one collection into the library, or every collection in the wiki when no positional is given. Pass `--source` to bootstrap a missing YAML (single-collection mode only); `--wizard` routes the bootstrap through `collection_author_agent`. Honors `Collection.scraper_cmd` (bespoke scrapers via `module:attr` / `path.py:attr`) and routes REGISTRY-registered source formats (sphinx / liquid / bespoke) through their builders before falling back to `format_dispatch`. Exits non-zero when the batch reports any `errors`.
- `lies migrate ingest-to-library [--dry-run|--apply]` — move wiki-resident ingests into the library. Backup duplicates at `<wiki>/.lies/migration-backup/<date>/`. `--dry-run` previews the moves; `--apply` performs one atomic commit per collection (cross-process flock, snapshot/restore on failure) and registers each library-side collection with qmd.
- `lies reindex --reconcile` — sync each collection.
- `lies collections list|show|modify` — manage collection configs (modify writes immediately; see `--help`).

## License

[MIT](LICENSE).

## Project links

- [Changelog](CHANGELOG.md)
- [Agent instructions](AGENTS.md)
- [License](LICENSE)
