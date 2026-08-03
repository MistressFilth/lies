# AGENTS.md

Source of truth for LLM agents working in the `lies` repository.

## Pre-PR checklist

Before opening or merging a PR, the agent MUST:

1. **Keep versioning bumps adherent to SemVer.** Bump the appropriate segment
   for the nature of the change. Update every version surface — see
   `~/.claude/rules/versioning.md`. In this repo those are `pyproject.toml`
   and `src/lies/__init__.py`.
2. **Keep `CHANGELOG.md` up to date.** Add an entry under the in-progress or
   new release section describing the change.
3. **Keep `README.md` up to date.** New commands, new config options, new
   install steps, behavior changes — all reflected in the README.
4. **Keep `docs/` up to date.** If the change is reflected there as well,
   keep it in sync.

## Pre-existing issues

Treat any "pre-existing" issue — one already on `main`, in the issue tracker,
or referenced in TODO/FIXME/XXX — **as if it is your own issue to solve**. Do
not dismiss as out-of-scope, historical, or someone else's problem. The first
encounter is yours; resolve or escalate.

## Project context

**LIES** — Library of Inconsistent Explanations & Sources. A
Karpathy-pattern LLM wiki: a `pydantic-ai`-harness agent maintains a
git-backed wiki of interlinked markdown files over a corpus of raw
sources. The schema (a per-wiki markdown file) defines page types,
conventions, and workflows. The human curates sources and asks
questions; the agent does all bookkeeping.

## Source layout

```
src/lies/
├── agents/          # sub-agent prompt YAMLs (source-reader, page-writer, ...)
├── capabilities/    # harness capability adapters (CodeMode, Memory, Planning, ...)
│   └── memory.py    # Harness Memory capability; per-wiki namespace via WikiIdentity
├── cli.py           # Typer CLI entrypoint (init / ingest / query / lint / mcp / REPL)
├── config.py        # env-driven config (model, wiki root, log level)
├── mcp/             # FastMCP server (src/lies/mcp/server.py) — thin adapter
│                    # around WikiMemoryService; tools: init_wiki, ingest_source,
│                    # query, lint, wiki_search, wiki_read
├── memory/          # invisible-memory layer (see below)
├── orchestrator.py  # top-level Orchestrator; owns cross-cutting capabilities
├── qmd/             # qmd CLI + MCP adapters
├── query/           # index.md parser + answer synthesizer
├── schema/          # default schema + loader
├── utils/           # logging, shell helpers
└── wiki/            # git + layout primitives
```

Tests mirror the layout:

```
tests/
├── unit/            # unit tests
├── integration/     # end-to-end tests (this project uses tests/integration/;
│                    # tests/features/ is also accepted by the Makefile)
├── mcp/             # FastMCP server tests
├── fixtures/        # shared fixture assets
└── conftest.py
```

## Invisible memory layer

`src/lies/memory/` is the invisible-memory layer:

- `WikiMemoryService` (in `service.py`) is the **single owner** of wiki
  mutation for memory: it validates plans, applies operations, snapshots
  the working tree, commits atomically, restores on failure, and refreshes
  the qmd derived index.
- `capabilities/memory.py` exposes this through the harness `Memory`
  capability with a per-wiki namespace derived from `WikiIdentity` (so
  two wikis against the same install do not share state).
- The Pydantic AI main agent reads through `wiki_search` and `wiki_read`
  tools; the FastMCP server exposes the same tools plus an expanded
  `query` response (`citations`, `pages_read`, `changed_pages`).
- After the answer, a `MemoryEnricher` sub-agent proposes a structured
  `MemoryPlan` only when evidence warrants it.
- The `EnrichmentQueue` (in `src/lies/memory/retry.py`) is a per-session,
  in-memory FIFO that retries transient `WikiMemoryService.apply_plan`
  failures (`WikiLockBusy`, `WikiWriteConflict`, `WikiCommitFailed`) at the
  start of the next turn. Capped at 3 attempts; deferred items surface as
  `(memory: deferred after 3 attempts — <reason>)` in the next receipt.

The lint repair workflow uses a separate `repair_agent` (in `src/lies/agents/repair.py`) that consumes a `LintReport` and emits a structured `RepairPlan`. The orchestrator applies the plan through `WikiMemoryService.apply_repair_plan`, which routes through the same cross-process flock and atomic-commit envelope as memory plans. The 4 primitives (`CreateStub`, `AppendLink`, `UpdateIndex`, `AppendEvidence`) map onto existing memory operations. The agent never emits ops for `safe_to_fix=False` findings; those stay in the report verbatim. The CLI flag is `lies lint --fix`; the FastMCP toggle is `lint(fix=True)`.

## Quality gates

`make check` runs `lint + typecheck + format`. `make test` runs the full
pytest suite. Pre-commit hooks wrap the same targets so a commit that
lands in the repo has already passed all gates.

## References

- Project overview: @README.md
- Makefile targets: @Makefile
- Release notes: @CHANGELOG.md
- Memory feature design: project notes at `~/code/project-notes/lies/superpowers/specs/2026-07-29-invisible-persistent-memory-design.md`
- MCP server design: project notes at `~/code/project-notes/lies/superpowers/specs/2026-07-27-lies-mcp-design.md`
- Invisible-memory plan: project notes at `~/code/project-notes/lies/superpowers/plans/2026-07-29-invisible-persistent-memory.md`
- MCP plan: project notes at `~/code/project-notes/lies/superpowers/plans/2026-07-27-lies-mcp.md`

For shared standards (versioning, changelog, pre-commit, repo layout,
Conventional Commits), see `~/.claude/rules/`.
