# Changelog

All notable changes to LIES are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/) adapted for
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed
- `lies lint` and `lies lint --fix` now see all six lint categories (contradiction, stale, orphan, missing_page, missing_xref, data_gap). The linter sub-agent's structured `LintReport` flows through `Orchestrator.run_lint` and is union'd with the deterministic host shell; the LLM is the source for the LLM-only categories and the shell remains the safety net when no model key is available. The deterministic shell also gained `missing_xref` and `missing_page` checks so an offline lint still finds the mechanical categories.
- CI and local hooks now run the Makefile-backed Ruff, ty, formatting, and full test gates.
- README now carries repository status, CI badge, development commands, and required project links.
- Pinned GitHub Actions to `actions/checkout@v7`, `actions/setup-python@v7`, and `astral-sh/setup-uv@v9`; workflow fetches full history for compliance checks.
- `AGENTS.md` references now point to project notes for the internal design and plan documents.

### Fixed
- Removed stale mypy commands and configuration after the repository moved to ty.
- Added the MIT license declared by package metadata.
- Registered the integration-test pytest marker so full-suite runs emit no unknown-marker warning.

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
