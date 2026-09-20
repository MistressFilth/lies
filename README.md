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

### QMD daemon recycle

`lies` recycles the qmd daemon automatically when a tool call
surfaces a transport error (qexpander cold-start wedge, daemon down,
or fastmcp-wrapped timeout). The recycle path is wrapped around the
underlying `MCPToolset` via `QmdRecycleToolset`; see
`src/lies/qmd/mcp.py` for the ReadTimeout-vs-TransportError
distinction. Explicit timeouts (`connect=2.0, read=60.0, write=10.0`)
are set via `httpx_client_factory` so a wedged daemon surfaces
within one budget window.

Manual operator trigger:

```bash
lies flock qmd recycle --name <wiki> --ready-timeout 30
```

If the daemon is serving a stale index (any wiki or library wrote to
disk since the daemon was spawned), `ensure_qmd_daemon` reaps and
respawns it on the next call. The `LibraryWriter` envelope touches
the global `<XDG_CACHE_HOME>/qmd/last-write-marker` sentinel on every
successful commit; `ensure_qmd_daemon` reads its mtime to detect
staleness. The construction-time check in `QmdCapability.as_capability`
closes the gap between out-of-band writes and the agent's first search:
if the marker indicates staleness when the capability is built, the
daemon is reaped and respawned before the native toolset is advertised.

The daemon has no authentication, so `up` and the internal `_serve`
command refuse non-loopback bind hosts. Put an authenticated reverse
proxy in front if remote access is required.

After registration, Claude Code sees these tools:

- `init_wiki(name)` — bootstrap a new wiki by name (creates XDG role-routed dirs).
- `query(question, name?)` — synthesized answer (structured result
  with `fallback_used`, `fallback_reason`, and `citations:
  list[Citation]` where each `Citation` carries a `source:
  "library" | "wiki"` discriminator, plus optional `line` and
  `section` so each footnote can point at the passage a claim relied on;
  library citations are the primary source of truth, wiki citations are
  supplementary). The synthesized answer ends with a `Footnotes:` block;
  each line reads `[^N]: [title](path#L<line>) — <section>`.
- `answer(question, name?)` — same synthesized answer body as plain
  text. Use this when the chat surface needs to render the answer
  verbatim (the structured `query` tool returns a JSON envelope that
  some surfaces hide behind collapsible blocks).
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

Each page type carries a set of required `## <Heading>` sections (see
`src/lies/schema/default_schema.md` → "Section contract"). The CLI
and MCP `file_knowledge` refuse writes that omit them; `lies lint`
surfaces them as `missing_required_section` findings
(`safe_to_fix=False`). Override per-wiki via `<wiki>/schema.md`. The
match is literal-substring — `## Evidence` matches, but `### Evidence`,
`##Evidence`, and `## evidence` do not.

## Tag-filter language

Filter `lies query` to library collections by name. `&` binds tighter
than `|`; `-tag` excludes; quoted tags (`+"airflow provider"`) allow
spaces.

```bash
lies query +airflow what are DAGs?
lies query +airflow&amazon -python what connectors?
lies query --tag-expr "airflow|spark" --exclude-tag aws what is X?
```

**The library is the source of truth for collections.** Library
collections live as directories under
`$XDG_DATA_HOME/lies/library/collections/<name>/`; they are canonical
source docs, not wikis, and carry no per-collection yaml-declared
tags. The addressable tag set is the set of library-collection
directory names — `+airflow` matches the airflow library collection,
`+provider` matches nothing because no library collection is named
`provider`. Wikis do not own collections; a wiki's local yaml
configs are legacy and not consulted for tag resolution. A
`+c:opencode` filter resolves from any wiki because the opencode
collection lives in the library.

The MCP `query` tool accepts `tag_expr` and `exclude_tags` (size ≤ 1)
kwargs. `SynthesizedMcpAnswer.searched_scope` reports the resolved
library-collection set; with no filter, it reports every library
collection.

When the active wiki's `tag_expr` references a collection the
library does not declare, the error surfaces the library's actual
collection list:

```
unknown tag: 'opencode'
the library's collections: example_a, example_b, example_c
```

When the library has not been initialized, the error names the gap
so the operator knows to initialize the library before filtering:

```
unknown tag: 'opencode'
the library is not initialized; collections live in the library, not in wikis.
```

When the library is initialized but has zero collections (empty
`collections_root`), the error tells the operator to ingest first:

```
unknown tag: 'opencode'
the library has no collections; ingest something first (see `lies ingest --help`) before querying with tag filters.
```

Notes on the CLI parser: Typer is configured with
`ignore_unknown_options=True` so the leading `+tag...` token chain
reaches `parse_query_argv`. The trade-off is that an unknown flag
(e.g. a typo like `--tagge` instead of `--tag`) is not rejected at the
Typer layer — it lands in the question text verbatim. Use
`--tag-expr` / `--exclude-tag` for an explicit, Typer-validated form.

### Qualifier prefixes (`t:` / `c:`)

Both prefixes are accepted but currently collapse to the same
answer: only the library-collection directory name is addressable, so
`+t:airflow` and `+c:airflow` both match only the airflow collection.

- `t:foo` (or no prefix): match the library collection named `foo`.
- `c:foo`: match the library collection named `foo` (strict name).

The prefix survives the parser so future tag metadata (per-collection
frontmatter, etc.) can reintroduce the `t:` / `c:` distinction
without a grammar change. Both include and exclude atoms accept the
prefixes; the same prefixes work in `mcp_query(tag_expr=...,
exclude_tags=...)`.

### Output formats

`lies query` accepts `--format=auto|md|table|marp` (default `auto`). The synthesizer picks the format at composition time based on the answer content; explicit values force re-synthesis with a constrained prompt if the auto-route differs.

- **`md`** (default fallback): plain markdown body.
- **`table`**: GFM pipe table with header + separator + data rows.
- **`marp`**: Marp-flavored markdown with `marp: true` frontmatter + slide breaks. When the `marp` CLI is on `$PATH`, the body is rendered to HTML at `${XDG_CACHE_HOME:-~/.cache}/lies/query-<timestamp>.html`. When `marp` is not installed, the body is written to a `.md` file and the path is printed with a render hint.

The format is also exposed on the MCP `query` response via the `format` field (`"md"`, `"table"`, or `"marp"`). Synthesis pages gain a `render_format` frontmatter field recording the body shape for future curators.

### `lies wiki provenance`

Lists every synthesised page's `derived_from` set, one row per page by default. Reads from the sqlite catalog — read-only; does not modify the wiki.

```bash
lies wiki provenance                       # TSV (slug, title, type, source_pkg, updated, csv(sources))
lies wiki provenance --json                # JSON array of objects (matches `lies catalog dump --json`)
lies wiki provenance --page <slug>         # drill in to one page; always JSON
lies wiki provenance --page <slug> --json  # explicit JSON
lies wiki provenance --orphan              # only pages whose derived_from cites a missing slug
```

Exit codes: `0` on success (including empty result), `2` when `--page <slug>` does not resolve or the slug is invalid. Errors print to stderr; JSON output stays machine-readable on stdout.

## Advanced

### Library collections

Collections are global library artifacts. Each named unit lives at
`$XDG_DATA_HOME/lies/library/collections/<slug>/` and contains:

- The deterministic scraped content (`<slug>.md` mirrors produced by
  `lies ingest`).
- A `config.yaml` holding the source URL, tags, scraper settings, and
  any builder-specific knobs (`Collection.config`).

No per-wiki collection config exists; any wiki can read any library
collection on demand. Wikis do not own collections — the library is the
source of truth for collection metadata.

Management surface (`lies library {list,show,where,new,modify,delete,enrich-tags}`):

- `list` / `show` / `where` — read-only: enumerate, summarize, print
  the on-disk path of every library collection.
- `new` — bootstrap a fresh collection config from `--source` (URL or
  path); does not ingest content (use `lies ingest` for that).
- `modify` — edit one or more `key=value` pairs (`--set KEY=VALUE`,
  with dotted keys like `config.<subkey>`) or replace the whole record
  via `--from-file PATH`. Writes immediately through an atomic
  tmp+rename envelope.
- `delete` — remove a collection's `config.yaml` (mirror files are not
  touched; re-ingest with `--force` to overwrite).
- `enrich-tags` — print one `lies library modify <slug> --set tags=...`
  hint per collection whose `tags` field is empty. Dry-run by default;
  the operator runs the printed commands manually. `--apply` is reserved
  for a future auto-apply and currently raises.

Inspect anything locally:

```bash
uv run lies library list
uv run lies library show htmx
uv run lies library where htmx
```

### Migrating from per-wiki configs

LIES 0.28.0 moves collection configs out of per-wiki YAMLs and into the
library. The migration is a one-shot, atomic-per-collection rename:

```bash
# Preview the moves (no writes):
uv run lies migrate-collection-configs --dry-run

# Apply the relocation:
uv run lies migrate-collection-configs --apply
```

`--apply` walks every wiki under `$XDG_CONFIG_HOME/lies/`, relocates
each `<wiki>/collections/<slug>.yaml` into the library at
`$XDG_DATA_HOME/lies/library/collections/<slug>/config.yaml`, and
deletes the source YAML on success. Each `save_config` is atomic per
slug via tmp+rename; the full plan is not atomic — a non-collision
failure in the middle leaves partial state (some library configs
written, all source YAMLs still on disk). The pre-flight check
surfaces two abort conditions before any write: duplicate slugs
across wikis, and library-side collisions (a partial-state re-run
where some library configs already exist). Pass `--force` to
overwrite the colliding library configs. A wiki without any per-wiki
collection YAMLs is a no-op success.

**Upgrading:** run `uv tool upgrade lies` to 0.28.0 first, then run
the migration above for each install before invoking any
`lies library` command — fresh installs on 0.28.0+ have no per-wiki
YAMLs and need no migration.

### Manual authoring (advanced)

The bootstrap path covers the common case (URL → bare YAML → sync). For
non-URL corpora or hand-tuned scrapers, write the library YAML
directly:

```bash
$EDITOR "$XDG_DATA_HOME/lies/library/collections/<name>/config.yaml"
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
# $XDG_DATA_HOME/lies/library/collections/liquid-theme/config.yaml
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

After the first successful sync, the collection's library record is
registered with the library registry and is addressable from any wiki.
Inspect with:

```bash
uv run lies library show htmx
# name=htmx source=https://... tags=['docs']
```

Re-runs are idempotent.

### Operator: qmd flock

When two or more lies processes touch qmd concurrently (CI
parallel `lies ingest`, `lies mcp` server concurrent commits,
`xargs -P N lies ingest`), the CUDA VMM pool reservation can race
and OOM-abort the qmd subprocess. lies serializes all `qmd_*` CLI
helpers through a site-wide flock at
`${XDG_STATE_HOME:-~/.local/state}/lies/qmd.lock` (override the
full path via `LIES_QMD_LOCK_PATH`).

Inspect a live holder:

```bash
lies flock qmd status
```

Force-reap the qmd flock envelope. Safe to run when no qmd
subprocess is running; if a live contender re-acquires the
lock between reap and retry, the command exits 1 with
`qmd flock still held; live contender survives force-repair`.

```bash
lies flock qmd force-repair
```

If a `qmd_*` call times out past the 30 s wait budget,
`LibraryWriter.commit` raises `QmdLockBusy` with the holder PID.
Wait a few seconds and retry, or inspect with
`lies flock qmd status` first.

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
- `LIES_QMD_LOCK_PATH` — overrides the default
  `${XDG_STATE_HOME:-~/.local/state}/lies/qmd.lock` lock path; useful for
  sandboxing or per-wiki isolation
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

`make check` runs ruff, ty, and ruff format (in that order); the
same checks fire on every commit via `.pre-commit-config.yaml`.
The pre-commit chain wraps `make unit-test`, so a commit that lands
in the repo has already passed the local gate.

`make lint-supyrliminal` runs `flake8 --select=SL,PYD`; the same
check fires on every commit via `.pre-commit-config.yaml` and in CI.
Supyrliminal is required — commits fail if a new SL/PYD finding
lands. The pre-existing 12 SL101 findings were resolved in
[#65](https://github.com/MistressFilth/lies/pull/65); the hook now
enforces zero findings.

### Test timing

`make time-unit-tests` runs the unit suite with
`--durations=0 --durations-min=0 -vv --tb=short --no-header` and
prints every test's wall-clock time, slowest first. Pass `--runslow`
to include the `@pytest.mark.slow` tests (CLI status rendering,
subprocess-bound daemon reap); without it, the slow tests are
skipped. `make time-features-tests` does the same for the
integration suite and short-circuits unless `INTEGRATION=1` is set
in the environment.

Integration tests are gated by `INTEGRATION=1` via a single
collection hook in `tests/integration/conftest.py`. Default
`make test` and `make check` skip them; CI runs them with
`INTEGRATION=1 uv run pytest tests/integration/`.

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
- `lies query [+tag[&|tag]...] [-tag] <question> [--collection NAME] [--no-file] [--force-file] [--tag-expr EXPR] [--exclude-tag TAG]`
  — ask a question of the wiki; answers are LLM-synthesized with
  `citations: list[Citation]` over qmd-retrieved pages (each
  `Citation` carries a `source: "library" | "wiki"` tag — library
  collections are the primary source of truth, wiki content is
  supplementary; the synthesis prompt instructs the LLM that
  library wins on conflict), falling back to the previous
  extractive output when no model is available. A leading `+tag`
  restricts the search to collections carrying that tag (atoms joined
  by `&` / `|`, with `&` binding tighter, e.g.
  `+airflow&provider|pyspark`); a following `-tag` excludes one tag.
  A collection's own name is always an addressable tag. Everything
  after the filter is the question, so bare `lies query what is X?`
  keeps working unchanged. `--tag-expr` (include body, no leading `+`)
  and `--exclude-tag` express the same filter without the prefix
  syntax and mirror the MCP tool's argument shape; when either is
  given the positional tokens are the question verbatim. A grammar
  error or a tag outside the registry exits 2. When the synthesizer
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

## Destructive reindex flags

`lies reindex` accepts four flags restored from PR #17:

- `--cleanup` — drop orphan qmd collections; vacuum FTS5 db. **Destructive**.
- `--all` — full rebuild incl cleanup. **Destructive**.
- `--force` — drop qmd's cache and rebuild index from scratch. Non-destructive.
- `--embed` — re-embed stale chunks. Non-destructive.

Destructive flags prompt for confirmation:

```
$ lies reindex --cleanup
Confirm destructive reindex (cleanup+drop orphans)? [y/N]: y
reconciled=False indexed=True embedded=False cleaned=True errors=[]
```

On a non-TTY (CI, scripts, piped output), the prompt refuses and exits 2 unless `--yes` is passed:

```
$ lies reindex --cleanup | tee log.txt
error: Confirm destructive reindex (cleanup+drop orphans)? [y/N] requires --yes when stdout is not a TTY
```

The MCP `reindex` tool mirrors this with `destructiveHint=True` and uses `ctx.elicit` for the confirmation.

## MCP server orientation

The LIES MCP server ships an orientation payload at every
`initialize` handshake plus six reference-prose MCP prompts:

- `instructions=` field: path/env facts, tool inventory,
  resource list, prompt index. The agent sees this on attach
  regardless of cwd.
- Prompts: `orient(wiki=...)`, `ingest(source=...)`,
  `query(question=...)`, `lint()`, `sync(collection=...)`,
  `file-back(wiki=...)`. Plus the pre-existing
  `ask_wiki(question)` and `ask_wiki_answer(question)`
  (= slash `/answer`) tool-call templates.

The payload lives at `src/lies/mcp/instructions.md` plus
`src/lies/mcp/prompts/*.md`. A pre-commit hook
(`tools/check_lies_commands.py`) blocks commits that
introduce unresolved `lies <cmd>` references.

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
- `lies library {list,show,where,new,modify,delete,enrich-tags}` — manage
  library collection configs (one directory per collection at
  `$XDG_DATA_HOME/lies/library/collections/<slug>/`, with the config in
  `config.yaml`). `list` / `show` / `where` are read-only; `new` /
  `modify` / `delete` mutate (modify writes immediately; see `--help`);
  `enrich-tags` prints one `lies library modify <slug> --set tags=<comma-separated>`
  hint per collection whose `tags` field is empty. Dry-run by default;
  the operator runs the printed commands manually. `--apply` is
  reserved for a future auto-apply and currently raises.
- `lies migrate-collection-configs [--dry-run|--apply]` — one-shot
  relocation of legacy per-wiki collection YAMLs
  (`$XDG_CONFIG_HOME/lies/<wiki>/collections/<slug>.yaml`) into the
  library (`$XDG_DATA_HOME/lies/library/collections/<slug>/config.yaml`).
  `--dry-run` previews the move list; `--apply` performs one atomic
  rename per collection and updates any wiki-side references. Run this
  after upgrading to 0.28.0 to lift your existing wikis onto the
  library-resident layout. See "Migrating from per-wiki configs"
  below.

## License

[MIT](LICENSE).

## Project links

- [Changelog](CHANGELOG.md)
- [Agent instructions](AGENTS.md)
- [License](LICENSE)
