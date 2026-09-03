# Changelog

All notable changes to LIES are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/) adapted for
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- JSONL receipt sidecar at `<wiki>/.lies/memory_plans.jsonl`. Each
  applied `MemoryPlan` appends one line (timestamp, commit SHA,
  rationale, pages, ops histogram, evidence count). Idempotent on
  commit SHA. Authoritative source is `git log`; the sidecar is
  rebuildable via `lies memory reconcile`.
- `lies memory` subcommand. Default shows the last 10 applied
  plans; `--limit`, `--pages`, `--ops`, `--since`, `--json` filter
  the output. `lies memory reconcile` rebuilds the sidecar from
  `git log --grep='^memory:'`. `lies memory truncate --keep N`
  caps the file (refuses `--keep` ≤0; refuses `--keep > count`
  without `--force`).
- MCP `wiki_changes` tool + `wiki://memory-changes` resource. The
  tool returns structured `MemoryPlanRecord`s; the resource returns
  formatted text. Both read the JSONL sidecar.
- `lies status --memory-limit N` flag. Augments `lies status`
  output with a "recent invisible writes" section.
- `--wizard` flag on `lies ingest`, `lies sync`, and `lies ingest-source`:
  when the collection YAML is missing, route the bootstrap through
  `collection_author_agent` instead of the bare scaffold. Requires a TTY.
- Auto-init wiki: `ingest`, `sync`, and `ingest-source` now create the wiki
  (via the same code path as `lies init`) when `--name` references an
  unregistered wiki. Idempotent.
- `lies.collections.bootstrap.bootstrap_collection`: idempotent YAML scaffold
  + collision refusal + wizard opt-in. New public primitive.
- `lies.collections.bootstrap.ensure_wiki`: resolve-or-auto-init helper.
- `synthesis_used` / `synthesis_reason` on query results, reported by
  `lies query` and the MCP `query` tool. They describe whether the LLM
  synthesizer or the extractive fallback wrote the answer, independently
  of `fallback_used`, which describes retrieval.
- `lies.query.retrieve_pages`: the shared retrieval path (qmd, falling
  back to `wiki/index.md`) behind both the extractive synthesizer and
  LLM synthesis. New public primitive.
- F2: single-source ingest (`lies ingest-source`) now runs the LLM
  round-trip through `source_reader_agent` and `page_writer_agent`;
  pages land at `wiki/<collection>/<file>` via
  `WikiMemoryService.apply_plan`. New `--no-llm` flag preserves the
  pre-F2 `sync_collection` shim behavior for operators who prefer
  bulk-scrape semantics. New `IngestQuarantined` /
  `IngestSourceUnreachable` errors mirror the existing
  `WikiMemoryError` rendering. `MemoryPlan` gains a `PageDelete`
  variant and an optional `tag` field for `log.md` attribution.

### Changed

- README lead rewritten to lead with the RAG contrast and the
  three-layer abstraction. The "Invisible memory" section now
  appears in the first third of the document. Prose regression
  gate: temporal markers ≤16, sentence-length std ≤11, Flesch
  ≥49.8.
- `--help` banner now names the maintenance loop and the three
  layers (replaces the 11-word "Library of Inconsistent
  Explanations & Sources" tagline).
- `default_schema.md` gains an "Invisible maintenance contract"
  section between Page types and Frontmatter, naming the
  `MemoryPlan` flow and the sidecar contract for the agent.
- `WikiMemoryService.apply_plan` now appends a receipt to the
  JSONL sidecar after the git commit lands, before qmd refresh.
  Sidecar append failure is non-fatal and surfaces in the
  receipt's `errors`. The commit message now includes
  `Pages:`/`Ops:`/`Evidence:` trailers for reconcile parsing.
- `lies ingest <coll>` and `lies sync <coll>` now bootstrap a missing
  collection YAML instead of raising `CollectionNotFound`. Help text
  updated to match. Refuses with `CollectionMismatch` if the existing
  YAML's source differs from `--source`.
- `lies ingest-source <source>` now requires `--collection NAME` (hard
  cutover). The legacy bypass of the collection YAML is gone; the command
  registers a YAML like `ingest` and `sync`.
- `pid_alive(pid)` (public, in `lies.etl.heartbeat`) now returns
  `Literal["alive","dead","indeterminate"]` instead of `bool`. Internal
  only — no CLI/MCP surface change.
- `pid_alive_fn` parameter to `acquire_create_lock` widens from
  `Callable[[int], bool]` to `Callable[[int], Literal["alive","dead","indeterminate"]]`.
- `AcquireResult.status` Literal gains `"indeterminate"`; populated
  fields (`holder_pid`, `holder_started_at`) unchanged.
- `lies query` and the MCP `query` tool now synthesize answers through
  `query_synthesizer_agent` instead of returning extractive excerpt
  bullets. The agent reads the full text of every retrieved page, cites
  its claims, surfaces disagreements between pages, and says what the
  wiki does not know. When the model is unavailable the previous
  extractive output is returned unchanged and `synthesis_used` is False.
- F3 file-back loop: when the synthesizer marks an answer
  `should_file`, `lies query` durably files it as a wiki page under
  `wiki/<collection>/synthesis/<file>` via
  `WikiMemoryService.apply_plan`. Three new flags surface the loop on
  the CLI: `--collection NAME` (where the page lands; required to
  actually write), `--no-file` (skip the loop even if the agent wants
  to file), `--force-file` (write regardless of the agent's verdict).
  Success prints a `(synthesis: durably filed - <op>: <path>)`
  receipt; failure prints `(synthesis: error — <reason>)`. Missing
  collection with `should_file=True` is recorded as a
  `synthesis_reason` note instead of erroring, so a misconfigured
  flag never costs the synthesized answer.

### Removed

- Implicit "creates collection if missing" promise from `ingest` /
  `sync` short-help text (replaced with accurate description).
- `lies ingest-source <source>` (no `--collection`) is no longer accepted;
  calls fail at the Typer layer with a missing-argument error.

### Fixed
- EPERM + stale heartbeat no longer triggers an unauthorized reap. The
  pid_alive classifier is now tri-state; an EPERM contender with a stale
  heartbeat returns `AcquireResult(status="indeterminate")` which
  callers translate to a new `WikiFlockIndeterminate` exception with an
  operator-actionable message ("Run `lies flock <name> force-repair`").
  Closes the M2 spec/implementation gap from PR #29's whole-branch
  review.
- Top-level `--name` removed. Subcommand `--name` is the only path;
  the REPL reads the wiki from `$LIES_WIKI_NAME` via `get_wiki_name()`.
- `PageDelete` ops now land in git. Previously, `_collect_commit_files`
  filtered staging candidates by `.exists()`, so the path was dropped
  after `_apply_operations` unlinked the file; the deletion stayed as an
  uncommitted `D` entry and the next `apply_plan`'s snapshot/restore
  resurrected the file. The candidate list now derives from the
  `PageReference` list returned by `_apply_operations`, so successful
  deletes are staged even after `unlink`. No-op deletes (file never
  existed) remain absent from the list so `git add` is never asked to
  stage a never-existed path.
- F2 whole-branch follow-up fixes (review of `run_ingest` /
  `WikiMemoryService`): the orchestrator's `_sha_lookup` now strips
  the `wiki/` prefix before reading, so an UPDATE on a prefixed
  page-writer path computes the real on-disk hash instead of `""`;
  `WikiMemoryService.validate_plan` strips the same prefix before
  reading so validate and apply agree on the resolved file; the
  system-file guard now normalizes `op.path` by stripping `wiki/`
  twice, so a `wiki/wiki/log.md` or `wiki/wiki/index.md` input is
  rejected as a system file instead of writing a shadow copy
  outside `append_log_entry`'s awareness; `run_ingest` discards
  (not leaks) the pre-ingest stash entry when
  `IngestSourceUnreachable` is raised. All four findings carry
  regression tests under
  `tests/integration/test_run_ingest_end_to_end.py` and
  `tests/unit/memory/test_service.py`.
- All Python 2 `except X, Y:` clauses parenthesized to the Python 3
  tuple form `except (X, Y):` across `src/` (orchestrator, memory
  service, mcp server, scrapers, utils, cli, schema, etl, etc.).
  ruff 0.16's formatter silently reverts the parens on py314
  (upstream issue #26449), so `ruff-pre-commit` is pinned to
  `v0.14.14` and `pyproject.toml` constrains `ruff<0.15` until
  upstream ships the fix.

## [0.11.1] - 2026-08-24

### Added
- After every successful `lies ingest`, `lies sync`, or
  `lies reindex --reconcile`, qmd's collection registration now
  points at the live wiki path (fixes a path-staleness bug introduced
  by the XDG migration in #33), the qmd index is refreshed, and
  per-collection vector embeddings are generated via
  `qmd embed -c <name>`. `qmd_collection_add_if_missing` is replaced
  by `qmd_collection_add_or_update` in the WRITING stage's post-commit
  hook; `qmd_embed` is the new wrapper. All three hooks stay non-fatal:
  a failure logs a stderr warning and the wiki commit stands. The
  embedding model is not pre-checked; `lies status` shows `Pending: N
  need embedding` if the model is unavailable. `lies reindex` (without
  `--reconcile`) is unchanged -- it stays a pure `qmd update`.

### Changed
- `lies --help` startup is ~2.7x faster (0.33s → 0.12s on this machine)
  by deferring two transitive cost centers off the bare CLI import path.
  `utils.logging` now imports `logfire` lazily inside
  `configure_logging()` only when `LOGFIRE_TOKEN` is set; without the
  token we never load its opentelemetry / requests / markdown_it / attr
  chain. `cli.operator` no longer imports `lies.mcp.daemon` at module
  top — that import pulled in `pydantic`, whose `pydantic.fields` plugin
  loader instantiates the logfire plugin regardless of the env var.
  The module is now imported inside the `mcp _serve / up / down / status`
  command bodies, and `_serve` + `up` use literal `typer.Option`
  defaults so the daemon module is not needed at decorator time.
- The WRITING stage now lands pages under `wiki/<collection>/<path>`
  (one subdirectory per collection) and registers each qmd collection
  against that subdirectory instead of the empty
  `raw/<collection>` directory. Previously the registration indexed
  zero files because raw files live elsewhere; the only collections
  that indexed anything were the ones whose `~/.config/qmd/index.yml`
  entries happened to point at `wiki.wiki_dir` already. New
  collections are silently broken no longer. `WikiCollectionRef.root`
  was a stale pointer at the raw dir; it now points at the
  per-collection wiki subdir so it actually describes where the
  pages live. Existing flat-layout pages in `wiki/` are left in place
  (orphaned, no manifest reference); the next `lies ingest` for a
  collection writes fresh files into the new subdir.

### Fixed
- `atomic_commit` now returns `None` instead of raising
  `CommitError("nothing to commit")` when the working tree matches
  HEAD after staging (e.g. a re-ingest of an unchanged collection).
  `WikiMemoryService.apply_plan` discarded the return value and
  unconditionally refreshed qmd + returned a `MemoryReceipt` carrying
  the in-memory `changed_pages`, falsely claiming the operations were
  applied at the git level. It now captures the return value, restores
  the working tree, and routes through `_empty_receipt()` when the
  commit was a no-op — so a no-op plan never claims git-level changes
  that did not land.

## [0.10.4] - 2026-08-22

### Changed
- `lies --help` (and other CLI startup paths) no longer load
  `pydantic_ai` / `fastmcp` transitively. The flock-age ceiling
  constant was duplicated as `MAX_FLOCK_AGE_S` in
  `lies.memory.service`; that definition moved to
  `lies.utils.exclusive` (where `MAX_FLOCK_AGE_S_DEFAULT` lived)
  and `memory/service.py` now imports it from the leaf. `cli/_helpers`
  follows the new path. `rich.console.Console` + `rich.markdown`
  imports also move out of `cli/query.py` module top into the two
  command bodies that actually use them. `import lies.cli` drops from
  ~1.28s to ~0.22s; `lies --help` wall-clock drops from ~1.07s to
  ~0.36s on this machine.

## [0.10.3] - 2026-08-22

### Changed
- `src/lies/cli.py` (1601 lines) is split into a `src/lies/cli/` package
  with one file per panel (`_core`, `ingestion`, `query`, `operator`,
  `collections`) plus cross-group helpers in `_helpers` and a `__main__`
  shim for `python -m lies.cli`. Heavy dependencies (`lies.orchestrator`,
  `lies.providers.{bootstrap,ops}`, `lies.scrapers.base`,
  `lies.qmd.qmd_status`, `lies.wiki.{git,layout}`) move from module-top
  eager imports to function-body lazy imports. `import lies.cli` no
  longer pulls in the orchestrator / pydantic-ai stack / anthropic SDK,
  so `lies --help` wall time drops from ~1.7s to ~1.1s on this machine.

## [0.10.2] - 2026-08-16

### Added
- Restore the `ahocorasick_rs>=1.0` Aho-Corasick fast-path inside
  `WikiLinkResolver`. The dep was removed in v0.9.2 (PR #22, commit
  `633a374`) because its wheel was missing on Python 3.13+ and the
  sdist was malformed; the wrapper's 1.0.3 release now ships prebuilt
  wheels including Python 3.14 (the new floor). When the wheel is
  absent, the resolver falls through to the dict-substring path
  unchanged — `TestResolverImportFallback` pins the contract that
  both branches return bit-identical results, deduplicated through
  `set`. Four tests re-added: `TestResolverUsesAhoCorasick` (2),
  `TestResolverImportFallback` (1), and
  `test_dict_fallback_restored_after_force_fail` (a 4th
  state-cleanliness guard beyond the original brief). Existing
  longest-match test unchanged.
- `python -m lies …` now works alongside the `lies` console script, via a minimal `src/lies/__main__.py` that delegates to `lies.cli:app`. The console-script entry point is unchanged.
- `lies providers init` interactive wizard (six subcommands under
  `lies providers …`). Opt-in, refuses to overwrite an existing
  `providers.toml` unless `--force`; companion commands
  (`add` / `set-default` / `assign` / `unassign` / `check`) cover every
  in-place edit. `--check-connection` flag and `lies providers check`
  ping every configured provider whose key is set; optional
  `--write-env-file PATH` captures current env values into a
  `chmod 600` file. First-run hint on `lies config / init / mcp up`
  when `sys.stdout.isatty()` and `providers.toml` is missing. New
  modules `src/lies/providers/{editor,bootstrap,ops}.py`; no new
  runtime or test deps.
- `lies config` and `lies collections show <name>` now surface the resolved effective language for the wiki. Resolution chain: `LIES_LANG` env var (highest priority) → `$XDG_CONFIG_HOME/lies/<name>/lies.toml` `[settings].lang` → default `en`. Per-collection `language` (set on the collection YAML) overrides wiki-global. Invalid values produce a stderr warning + defaults; no fatal errors.
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
- `lies providers init` wizard reordered: the catalog step now runs
  before the `default_model` prompt so a single wizard pass can set
  any model whose provider is freshly declared. Removed the implicit
  `[providers.anthropic]` seed — the catalog starts empty and the
  first provider declared is canonical. The catalog step is now
  required: at least one provider must be declared before write;
  back-out via `^C` only. Strict-validation contract for
  `default_model` and `set_agents` preserved.
- Python floor bumped from `>=3.10` to `>=3.14,<3.15` to align with the
  Python versions where the restored `ahocorasick_rs` 1.0.x wheels are
  available prebuilt and where the project's typecheck / lint toolchain
  is now stable. Operators on Python 3.10–3.13 must upgrade or stay on
  v0.9.3.
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
- `lies collections modify <name>` now edits a collection record and
  writes immediately to the wiki's collections config directory
  (`<config_root>/collections/<name>.yaml`). New flags: `--from-file PATH`
  (whole-record YAML) and `--set KEY=VALUE` (one-off tweaks; dotted keys
  support `config.<subkey>`). The documented "in-memory edit; persists
  via `lies commit`" surface is dropped — `lies commit` is not planned.
- `save_collection` writes atomically via sibling tmp + `os.replace` +
  fsync (mirrors `Registry.save`). New typed error `CollectionWriteFailed`
  raised on IO failure.

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
- The `lies reindex --embed`, `--cleanup`, `--force`, and `--all` flags, plus the underlying `qmd_embed`/`qmd_cleanup` library stubs. Upstream `qmd` exposes no embed or cleanup subcommand, and the flags existed only as no-op placeholders that printed a stderr warning. `lies reindex --reconcile` remains. Note: this is technically a SemVer-major removal of documented CLI surface; it ships in the 0.8.0 minor bump rather than a SemVer-major, per maintainer directive — operators scripting the removed flags will see a typer exit-code-2 "no such option" error.

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
- `lies sync <collection>` now refreshes the qmd derived index and regenerates `wiki/index.md` on every successful write. The WRITE stage runs three non-fatal post-commit hooks: `qmd collection add` (idempotent — treats `Collection '<name>' already exists` as success), `qmd update` (cwd = wiki root, matching the `WikiMemoryService` envelope), and `rebuild_index(wiki)`. The hooks fire only when files were actually written; pure-skip syncs leave qmd and the catalog untouched. `qmd_collection_add_if_missing` lives in `src/lies/qmd/cli.py` and is the only place that recognises the "already exists" stderr. The previous separate `QMD_UPDATE` pipeline stage was removed to avoid double-indexing: hooks now run exactly once, inside WRITE. Symptom that pinned the bug: 187-chunk sync leaving qmd reporting `100% need embeddings` and `lies query` returning empty despite `qmd query` returning 88% match.
- `runtime_dir()` no longer raises `PermissionError` when `XDG_RUNTIME_DIR` (or the `LIES_XDG_RUNTIME_DIR` override) points at an unwritable path. Both mkdir calls in `src/lies/xdg.py` now wrap in `try/except OSError` and fall through to `<state_home>/run` per the module's best-effort contract — the promise the docstring already made. Affects every CLI surface that resolves a wiki (`lies query / init / mcp status / config`); previously each invocation died before any user-visible work could start.
- `WikiNotRegistered` message now prints the per-wiki directory the resolver actually probed (`<data_home>/lies/<name>`) instead of the bare `<data_home>/<name>`, so operators reading "not registered at ..." can locate the missing directory on disk without guessing the `lies` segment. Targets `src/lies/errors.py` — one-line change, no API impact.
- `qmd_query` now normalizes the real qmd `--format json` shape by stripping the `qmd://<collection>/` URI prefix from each result's `file` field into a top-level `path` key, keeping the boundary contract documented on `qmd_query` truthful. Downstream `lies query` no longer drops hits on the floor and falls back to `wiki/index.md` even when qmd returns matches. The same latent shape defect in `memory/retrieval._from_qmd` is repaired transitively by the boundary normalization.

## [0.9.2] - 2026-08-11

### Removed
- Drop the `ahocorasick_rs` runtime dependency. `WikiLinkResolver.resolve` is now dict-only — equivalent correctness, simpler install on Python 3.13+ where the upstream 0.22.2 wheel is missing and the sdist is malformed. No behavior change for `lies lint` output. (Restored in [0.10.0].)

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
