# Changelog

All notable changes to LIES are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/) adapted for
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- LIES now follows the [XDG Base Directory Specification](https://specifications.freedesktop.org/basedir/latest/). Wiki content lives under `$XDG_DATA_HOME/lies/<name>/`; configuration under `$XDG_CONFIG_HOME/lies/<name>/`; runtime locks under `$XDG_RUNTIME_DIR/lies/<name>/`; logs/scratch/poison under `$XDG_STATE_HOME/lies/<name>/`; hashes/manifests under `$XDG_CACHE_HOME/lies/<name>/`. Override any root with `LIES_XDG_<NAME>`.
- `lies init <name>` — name-based initialization (no path argument). Creates all five role-routed XDG directories and `git init` the wiki root.
- `lies migrate-xdg <legacy-path> --name <name>` — one-shot migration of legacy `<path>/.lies/` into XDG role-routed directories. Idempotent; refuses on byte-mismatched conflicts.
- `lies migrate-xdg ... --force` now *quarantines* the conflicting source files at `<legacy-path>/.xdg-migration-conflicts/<rel>` instead of silently dropping them; the destination file is left untouched.
- `LiquidBuilder` for `source_format=liquid` collections. Pluggable
  `Collection.config["render_cmd"]` (`module:attr` import path, mirrors
  `scraper_cmd`) renders Liquid → HTML; the existing pandoc path
  converts the HTML to markdown. When `render_cmd` is omitted, the
  source is passed through unchanged (treated as already-rendered
  HTML). Per-doc quarantine on render failure mirrors `PDFBuilder`.
  Sets the slot reserved by the 2026-08-01 source-collection-builders
  spec.
- `lies mcp up` starts a detached streamable-http MCP daemon for the wiki
  (default `127.0.0.1:8737`, `--host` / `--port` / `--timeout` to override).
  The parent re-execs a hidden `_serve` subcommand in a new session, waits
  for the port to accept a connection, and only then writes
  `<wiki>/.lies/mcp.pid` — a reported success always means a live server.
- `lies mcp up` also ensures qmd's own daemon via the idempotent
  `qmd mcp --http --daemon`. qmd is a search backend, not a prerequisite:
  if it is missing or fails to start, the LIES daemon still comes up and a
  single warning goes to stderr. `--no-qmd` skips the step.
- The agent's qmd search now routes through that daemon instead of
  spawning a `qmd` subprocess per agent. Configured by
  `LIES_QMD_TRANSPORT` (default `http`, set `stdio` to opt out) and
  `LIES_QMD_URL` (default `http://127.0.0.1:8181`).
- `lies mcp down` stops the pidfile-tracked daemon, escalating SIGTERM to
  SIGKILL after `--grace` seconds. Stdio servers spawned by an MCP host are
  never touched, and qmd's daemon is never touched at all — it is
  machine-global and shared with other wikis and tools. A missing or stale
  record is a successful no-op.
- `lies mcp start` runs the server on stdio in the foreground. Bare
  `lies mcp` is unchanged and still does the same thing, so every
  already-registered MCP host keeps working.
- `lies mcp status` reports pid, URL, uptime, and log path, exiting 0 when
  running and 1 when stopped or stale (the `systemctl is-active`
  convention).
- `src/lies/utils/exclusive.py` holds the shared `O_CREAT | O_EXCL`
  create-lock and gitignore guard, extracted from `etl/heartbeat.py` and
  `memory/service.py`. Both call sites keep their original signatures.
- `WikiLayout.init` now gitignores `.lies/mcp.pid`, `.lies/mcp.pid.create`,
  and `.lies/mcp.log`; `lies mcp up` ensures the same entries for wikis
  created before this release.
- `$XDG_CONFIG_HOME/lies/providers.toml` for declaring providers and per-agent model assignments. TOML format with `[providers.<name>]` (anthropic or anthropic_compatible) and `[agents]` sections. Missing entries for agents in `AGENT_ROSTER` raise `ProviderConfigError` at config load.
- `minimax` provider example wired against `https://api.minimax.io/anthropic` for Anthropic-compatible inference. MiniMax clients are constructed directly via `AnthropicModel` + `AnthropicProvider` (pydantic-ai 2.18 has no public model-registry API).
- `LIES_<AGENT>_MODEL` env var precedence: a non-empty value for any agent in `AGENT_ROSTER` overrides the TOML `[agents]` entry. Useful for one-off model swaps without editing the config file.
- `lies config` now lists every agent in `AGENT_ROSTER` with its resolved model (string for built-in `anthropic:` prefixes, `AnthropicModel` for custom).
- Missing `providers.toml` is non-fatal: every agent falls back to the previous default (`anthropic:claude-opus-4-7`) and a single stderr warning names the expected path.

### Changed
- CLI flag `--wiki-root`/`-w` replaced by `--name` on every command. Default wiki name `default` (set `LIES_WIKI_NAME` to override).
- Orchestrator construction no longer reads `LIES_MODEL`. It loads user-level `providers.toml` (or every agent falls back to `anthropic:claude-opus-4-7` when the file is missing) and resolves one `Model | str` per agent.
- Agent factory signatures (`source_reader_agent`, `page_writer_agent`, `indexer_agent`, `linter_agent`, `query_synthesizer_agent`, `repair_agent`, `enricher_agent`) now accept `model: Model | str` instead of `model: str`.
- Wiki identity is a name (basename), not a path. Wikis are looked up under `$XDG_DATA_HOME/lies/<name>/`. Wiki roots elsewhere require migrating via `lies migrate-xdg` (or creating fresh via `lies init <name>`).
- The unauthenticated MCP daemon now refuses non-loopback bind hosts in
  both `lies mcp up` and the internal `_serve` command; remote access
  requires an authenticated reverse proxy.
- `lies lint` and `lies lint --fix` now see all six lint categories
  (contradiction, stale, orphan, missing_page, missing_xref, data_gap).
  The linter sub-agent's structured `LintReport` flows through
  `Orchestrator.run_lint` and is union'd with the deterministic host
  shell; the LLM is the source for the LLM-only categories and the
  shell remains the safety net when no model key is available. The
  deterministic shell also gained `missing_xref` and `missing_page`
  checks so an offline lint still finds the mechanical categories.
- CI and local hooks now run the Makefile-backed Ruff, ty, formatting, and full test gates.
- README now carries repository status, CI badge, development commands, and required project links.
- Pinned GitHub Actions to commit SHAs (`actions/checkout@3d3c42e…`, `actions/setup-python@5fda3b9…`, `astral-sh/setup-uv@c771a70e…`).
- `AGENTS.md` references now point to project notes for the internal design and plan documents.

### Removed
- `LIES_WIKI_ROOT` environment variable. Set `LIES_WIKI_NAME` instead.
- The `<wiki>/.lies/` directory. All state (locks, pid, log, schema, collections, hashes, telemetry, poison) routes to role-specific XDG directories.
- `WikiLayout.lies_dir`, `WikiLayout.schema_path`, `WikiLayout.memory_lock_path`, and all other `.lies/...` accessors. Use `Wiki` accessors instead.
- `utils.exclusive.ensure_gitignored` (no `.lies/` to gitignore).
- `scripts/worktree_lint.py` and the `make worktree-lint` target; the seven-invariants checker is a tool that asserted user-scope rule adherence.
- `tests/unit/test_repository_metadata.py` and its pytest-marker, GitHub-Actions SHA, and mypy-absence assertions.
- `make release` no longer runs `worktree-lint` as a prerequisite.
- CI workflow no longer fetches full git history (`fetch-depth: 0`); the only consumer was the dropped compliance test.
- `LIES_MODEL` env var. Set per-agent env vars (`LIES_ORCHESTRATOR_MODEL`, etc.) or edit `providers.toml` instead.
- `lies.config.get_model` and the `LIES_MODEL` constant in `src/lies/config.py`.

### Fixed
- `LiquidBuilder` now rejects empty pandoc output so failed conversions
  quarantine the document instead of emitting an empty page.
- Path-based Liquid `render_cmd` modules are now reused across builds so
  module-level renderer caches and state survive multiple documents.
- Agent's qmd search now degrades gracefully when the qmd daemon is
  unreachable. `QmdCapability` probes the daemon on every turn via
  `qmd_daemon_reachable`; reachable -> native `MCP(url=..., native=True,
  local=False)`, unreachable -> `MCP(local=factory)` whose factory returns
  an `MCPToolset` over the in-process `QmdFallbackMcp` server. Every
  fallback search result carries `degraded: True` plus a `fallback_reason`,
  and one stderr warning names the URL, the consequence, and the fix
  (`LIES_QMD_URL` or `qmd mcp --http --daemon`). `LIES_QMD_TRANSPORT=stdio`
  and the host `lies query` path are unchanged.
- Removed stale mypy commands and configuration after the repository moved to ty.
- Added the MIT license declared by package metadata.
- Registered the integration-test pytest marker so full-suite runs emit no unknown-marker warning.
- `WikiMemoryService.register_collection` now persists to `$XDG_STATE_HOME/lies/<name>/registry.json` so the registration survives process boundaries. `lies collections show` is truthful across processes; the ETL `REGISTERING` stage no longer silently re-registers on every sync. Stale entries (whose `collections/<id>.yaml` is missing) are dropped at load and never persisted. Writes are atomic via temp+rename; concurrent registers union rather than overwrite.

## [0.5.1] - 2026-08-03

### Added
- `validate_plan(plan, layout, findings)` in `src/lies/agents/repair_validation.py`. Catches `finding_index` out of range, ops against `safe_to_fix=False` findings, ops whose `pages` set does not intersect the referenced finding's pages, per-op filesystem checks (`CreateStub` on existing path, `AppendLink` to a missing `target_path` or `append_to`, `AppendEvidence` on a missing path), and silently drops redundant `UpdateIndex` operations. Atomic rejection (raises `WikiPlanInvalid`) for rules 1-4; the dropped-op indices surface as `redundant-index` entries in `RepairReceipt.skipped`.

### Changed
- `Orchestrator.run_lint(apply=True)` now calls `validate_plan` between the repair agent and `apply_repair_plan`. `WikiPlanInvalid` is mapped to a `RepairReceipt` with `errors=[...]` so the existing `_format_repair_section` surfaces the rejection.
- `Orchestrator._apply_repair_plan` accepts a `ValidatedRepairPlan` and rebuilds `applied_repair_kinds` from the post-drop `plan.operations` to keep its positional pairing with `memory_receipt.changed_pages`.
- `_format_repair_section` adds a `### Skipped (redundant)` block when the receipt's `skipped` list contains `redundant-index` entries.

## [0.4.0] - 2026-08-02

### Added
- New `src/lies/builders/` package with `Builder` ABC and `BuilderRegistry`. PDF (`PDFBuilder`, pdfplumber primary, pymupdf fallback), Sphinx (`SphinxBuilder`, includes/excludes/renames on `Collection.config`), HTML (`HTMLBuilder`, pandoc), and Bespoke (`BespokeBuilder`, dispatches by emitted `source_format`) builders. `source_format=liquid` raises `BuilderUnavailable` and per-doc quarantines — deferred to a follow-up.
- `Collection.config: dict[str, Any]` for builder-specific knobs. Round-trips through YAML.
- `WikiMemoryService.register_collection(ref)`, `.is_registered(id)`, `.registered_collections()` — in-memory only in v1.
- `SyncOrchestrator` runs a new `REGISTERING` state between `WRITING` and `QMD_UPDATE`; registers a `WikiCollectionRef` on first successful sync per collection. Idempotent. Failure is non-fatal.
- `lies collections new <name> --source <url> --prompt "..." [--apply]` drives a `CollectionAuthorAgent` sub-agent through one `rich.prompt` question at a time. Emits a `Collection` YAML on stdout; with `--apply` writes `<wiki>/.lies/collections/<name>.yaml`. No wiki mutation at author time.
- `lies collections show <name>` appends `status: registered|pending`.
- `Collection.scraper_cmd` is honored by the SCRAPE stage: `module:attr` or `path.py:attr` resolves to a `BaseScraper` instance via `importlib`. Bespoke modules live outside the repo.
- `lies collections show` reports registration status.
- New runtime dep: `pdfplumber`.
- `lies lint --fix` (CLI) and `lint(fix=True)` (FastMCP) consume the linter's `LintReport` and apply a structured `RepairPlan` through `WikiMemoryService`, gated by the finding's `safe_to_fix` flag. The 4 primitives (`CreateStub`, `AppendLink`, `UpdateIndex`, `AppendEvidence`) map onto existing memory operations; one atomic commit, one cross-process flock, full rollback on failure. Dry-run is the default.
- `lies ingest-source <source>` (CLI) preserves the original source-path ingestion surface alongside the new collection-aware `lies ingest <collection>`. The legacy form delegates to `Orchestrator.run_ingest` (the host-side atomic wrapper) and accepts the same `--wiki-root` override as the other commands.
- `SyncTelemetry` is now a context manager; `sync_helper.sync_collection` runs the pipeline inside `with SyncTelemetry(...)` so the log file handle closes on exception, not only on the happy path.
- New ETL pipeline (`src/lies/etl/`) with state-machine `SyncOrchestrator` driving the four stages `SCRAPE → NORMALIZE → WRITE → QMD_UPDATE`. Each stage threads `parsed_docs` through `StageResult` so docs processed upstream can be reused downstream without re-fetching.
- Three new packages: `etl/` (pipeline, stages, sync_helper, telemetry, cost, heartbeat, quarantine, query), `scrapers/` (`BaseScraper` ABC plus `GitHubScraper`, `WebScraper`, and `PDFScraper`), and `collections/` (`Collection` record + YAML loader, `Document` record with status enum, `HashManifest` read/write/compare/snapshot/restore, `ScraperManifest` read/write).
- Bulk wiki writes bypass `WikiMemoryService` in the WRITE stage and use `atomic_commit` directly; per-doc failures on filesystem errors are quarantined under `.lies/poison/<collection>/<path>` with a sidecar `.reason` file instead of failing the whole sync.
- New CLI subcommands `lies sync`, `lies ingest`, `lies reindex`, and `lies collections list|show|modify` extend `src/lies/cli.py`; each delegates to `etl/sync_helper.py` rather than re-implementing pipeline orchestration.
- Per-sync telemetry writes an NDJSON log alongside a parsable `SyncReceipt`; `CostBudget` caps each sync at 10 LLM calls and 500k tokens with explicit `record_counters` rejection of unknown counter names.
- Cross-process busy detection via a heartbeat file at `<wiki_root>/.lies/sync.lock` with stale-recovery on missed heartbeats; the lock file is gitignored and protected with an atomic `O_CREAT | O_EXCL` create on a sibling `.lies/sync.lock.create` to close the TOCTOU race.
- Pre-translate `StructuredIntent` plus a `qmd_syntax.translate` shim so the agent can pre-translate natural-language queries against the qmd surface before sending them to `qmd_query`.
- Single-shot Pandoc conversion wrapper that starts a fresh subprocess for each document because EOF is the CLI's only input boundary; PDF extraction via `pymupdf` (`extract_text` plus `extract_text_ocr`).
- New runtime dependency: `pymupdf>=1.24`.

### Fixed
- `lies reindex --embed` and `lies reindex --cleanup` now print a stderr warning explaining they are no-op placeholders (upstream `qmd` exposes no `embed`/`cleanup` subcommand). Exit code stays 0 to preserve the documented surface; users see a clear hint instead of a silent success.
- `sync_helper.acquire_heartbeat` no longer has a TOCTOU race. It now takes an atomic `O_CREAT | O_EXCL` create on a sibling `.lies/sync.lock.create` before reading or writing the heartbeat; two concurrent `lies sync` invocations cannot both succeed. The lock file is gitignored and the fd is closed in `release_heartbeat`. Stale create-lock files left behind by a crashed acquirer are now reclaimed via mtime check, with a recovery window equal to `MAX_SYNC_AGE_S`.
- `lies collections modify <name>` now raises `typer.BadParameter` instead of silently printing an "in-memory edit" message. The previous output claimed success while doing nothing; the new behavior matches the deferred status honestly.
- `sync_helper.sync_collection` docstring corrected: it errors if the collection YAML is missing rather than promising to auto-scaffold one (the LLM scraper generation flow remains deferred).
- `SyncOrchestrator.run` now records `started_at` before any stage transition so receipts always carry a non-`None` start timestamp.
- `SyncTelemetry.record_counters` was overwriting the in-memory counter on each call. It is now split into `record_counter(name, value)` (single counter, accumulates) and `record_counters(**fields)` (legacy batch shim that delegates); the pipeline records each stage's contribution so totals reflect the full run, not the last write.

## [0.2.0] - 2026-07-29

### Added
- Invisible persistent wiki memory: `WikiMemoryService` owns retrieval, validation, atomic mutation, git commit, and qmd refresh.
- Read tools `wiki_search` and `wiki_read` exposed to the Pydantic AI main agent and to the FastMCP server.
- `MemoryEnricher` sub-agent proposes a structured `MemoryPlan` from a bounded evidence envelope (user request, assistant answer, pages read, citations, current page metadata, active schema).
- Per-wiki Harness Memory namespace derived from `WikiIdentity` so two wikis against the same install do not share state.
- CLI default for free-form REPL commands routes through `Orchestrator.run_with_memory`; `--no-memory` preserves plain `Orchestrator.run`.
- FastMCP `wiki_search`, `wiki_read`, and expanded `query` response with `citations`, `pages_read`, and `changed_pages`.
- `EnrichmentQueue` retries transient `WikiMemoryService.apply_plan` failures (`WikiLockBusy`, `WikiWriteConflict`, `WikiCommitFailed`) at the start of the next turn, capped at 3 attempts. Deferred items surface in the next receipt as `(memory: deferred after 3 attempts — <reason>)`. Per-session, in-memory only.

### Changed
- `capabilities.memory` now requires a `wiki_root` argument (was optional).

### Fixed
- `WikiMemoryService.apply_plan` now passes an explicit `files=[...]` to `atomic_commit` so newly created pages and `wiki/log.md` lines are committed (was using `git add -u`, which dropped untracked files).
- Failure path now snapshots and restores the dirty tree on commit failure (was leaving partial writes behind).
- `hash_page` distinguishes missing file (returns `""`) from empty file (returns SHA-256 of empty string).
- `WikiMemoryService.apply_plan` now acquires a non-blocking cross-process `fcntl.flock` on `<wiki_root>/.lies/memory.lock` before mutating, raising the typed `WikiLockBusy` so concurrent processes cannot corrupt the working tree (was relying on the in-process `threading.Lock` only). `WikiLayout.init` and `WikiMemoryService` both ensure `.lies/memory.lock` is gitignored so `git stash push --include-untracked` (used by snapshot/restore) cannot unlink the inode behind a held flock.

## [0.1.0] - 2026-07-27

### Added
- Initial release: Karpathy-pattern LLM wiki with `pydantic-ai`-harness agent, FastMCP server, and CLI (init / ingest / query / lint / REPL).
