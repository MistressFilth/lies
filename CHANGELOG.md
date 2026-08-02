# Changelog

All notable changes to LIES are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/) adapted for
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- `lies lint --fix` (CLI) and `lint(fix=True)` (FastMCP) consume the linter's `LintReport` and apply a structured `RepairPlan` through `WikiMemoryService`, gated by the finding's `safe_to_fix` flag. The 4 primitives (`CreateStub`, `AppendLink`, `UpdateIndex`, `AppendEvidence`) map onto existing memory operations; one atomic commit, one cross-process flock, full rollback on failure. Dry-run is the default.
- `lies ingest-source <source>` (CLI) preserves the original source-path ingestion surface alongside the new collection-aware `lies ingest <collection>`. The legacy form delegates to `Orchestrator.run_ingest` (the host-side atomic wrapper) and accepts the same `--wiki-root` override as the other commands.
- `SyncTelemetry` is now a context manager; `sync_helper.sync_collection` runs the pipeline inside `with SyncTelemetry(...)` so the log file handle closes on exception, not only on the happy path.

### Fixed
- `lies reindex --embed` and `lies reindex --cleanup` now print a stderr warning explaining they are no-op placeholders (upstream `qmd` exposes no `embed`/`cleanup` subcommand). Exit code stays 0 to preserve the documented surface; users see a clear hint instead of a silent success.
- `sync_helper.acquire_heartbeat` no longer has a TOCTOU race. It now takes an atomic `O_CREAT | O_EXCL` create on a sibling `.lies/sync.lock.create` before reading or writing the heartbeat; two concurrent `lies sync` invocations cannot both succeed. The lock file is gitignored and the fd is closed in `release_heartbeat`.
- `lies collections modify <name>` now raises `typer.BadParameter` instead of silently printing an "in-memory edit" message. The previous output claimed success while doing nothing; the new behavior matches the deferred status honestly.
- `sync_helper.sync_collection` docstring corrected: it errors if the collection YAML is missing rather than promising to auto-scaffold one (the LLM scraper generation flow remains deferred).

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
