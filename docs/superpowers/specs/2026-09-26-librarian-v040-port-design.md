# Librarian v0.40 — Port ask's retrieval pipeline

**Status:** design approved, pending implementation
**Date:** 2026-09-26
**Target release:** v0.40.0
**Branch:** `librarian-v040`

## Problem

The current `synthesize` / `ground` pipeline produces shallow answers when the corpus has deep authoring guides alongside overview pages.

### Reproduction (sessions 2026-09-25/26)

User prompt: `/lies:answer +c:opencode|c:claude_code How do OpenCode and Claude Code compare with one another when making plugins?`

Corpus state at the time of the user's test:
- `opencode/v2/docs/build/plugins.md` exists (the actual OpenCode plugin-authoring guide, ~300 lines covering the `Plugin.define({ id, setup(ctx) })` API).
- `claude_code/plugins.md` exists (Claude Code's `Create plugins` page, covering manifest format and authoring workflow).

What the librarian returned (CC session `fde8d304`):
```
pages_read: [
  claude_code/features-overview.md,
  claude_code/plugin-hints.md,
  claude_code/it.md,
  claude_code/discover-plugins.md,
  claude_code/pt-br.md,
  claude_code/fr.md,
  claude_code/vs-code.md,
  claude_code/id.md,
  opencode/v2/docs/cli/plugins.md,
  claude_code/plugin-marketplaces.md
]
```

Zero hits on `opencode/v2/docs/build/plugins.md` or `claude_code/plugins.md`. qmd's BM25 ranking pushed German/Italian/French localized overviews and install/discover pages above the authoring guides for the words "compare" + "plugins".

### Root cause

Two structural problems:

1. **Per-collection fan-out with blind top-K reads.** `src/lies/mcp/grounding.py::_fanout_collections._one(name)` calls qmd once per collection with `limit=top_k` and returns the top-K hits per collection, then merges. The merged top-K is whichever collection had the highest TF-IDF scores for the query. The librarian LLM in `src/lies/agents/librarian.py` then blindly reads every hit — no snippet review between Search and Read.

2. **No snippet-review step.** The librarian LLM never sees the snippets that qmd returns before committing to reads. It cannot prefer "this snippet shows authoring content" over "this snippet shows install commands." Result: overview and localized pages get read at the expense of the authoring guides.

## End-state (the design goal)

User prompt: `/lies:answer +c:opencode|c:claude_code How do X and Y compare for Z?`

The system:

1. **Live registry check.** Librarian LLM reads `collections_read("list")` to discover registered collections, their tags, and scope keywords. No hardcoded maps.
2. **Filter as constraint.** Caller-supplied `+c:opencode|c:claude_code` is validated against the registry. Spelled-wrong tags surface as `unknown_tags`, not silent fallback.
3. **Hybrid single-batch search.** One qmd POST with `{searches: vec+lex, collections: [...], limit: 10}`. Single round-trip. Globally ranked across the union.
4. **Snippet review.** Librarian LLM gets ~10 hits with snippets. Decides which 3–5 to deep-read based on snippet content.
5. **Read.** Deep-reads only the chosen pages.
6. **Curate excerpts.** Librarian returns full-page bodies (not ≤200-char snippets) to the synthesizer.
7. **Synthesize.** Synthesizer LLM writes the answer with proper citations.

Steps 4–5 are the structural fix. Steps 1–3 make sure the LLM has the right candidates to choose from.

## Scope

Full port of `ask`'s retrieval pipeline into `lies`, with hard-cutover MCP surface renames. Single release (v0.40.0). No deprecation wrappers.

### What changes

| Module | Change |
|---|---|
| `src/lies/mcp/server.py` | Register `collections_read`, `search`, `read`, `ask`. Drop `wiki_search`, `wiki_read`, `wiki_catalog`, `synthesize`, `ground`, `ask_question`, `ask_ground_question`, `init_wiki`, all prompts. |
| `src/lies/mcp/grounding.py` | Rewrite `search()` as single-batch qmd call. Remove `_fanout_collections`, `_query_tagged_collections`, `_unscoped_fanout`. |
| `src/lies/mcp/synth.py` | Rewrite `ask()` as orchestration: librarian agent → synthesizer. Drop snippet→Span→PageExcerpt hydration. |
| `src/lies/agents/librarian.py` | Rewrite as 4-step pipeline. New `librarian_agent` system prompt. Tool list: `collections_read`, `search`, `read`. |
| `src/lies/agents/query_synthesizer.py` | Unchanged contract. |
| `src/lies/library/registry.py` | Add `collections_read`-friendly row shape. |
| `src/lies/mcp/instructions.md` | New tool surface documentation. |
| `docs/MCP_INSTRUCTIONS.md`, `docs/ROUTING.md`, `README.md` | New tool list. |
| `tests/fixtures/library/collections/` | Curated corpus: ~30 docs across 5 collections. |
| `tests/unit/mcp/test_*.py` | Rewrite against new tools. |
| `tests/unit/agents/test_librarian.py` | Rewrite for 4-step pipeline. |
| `tests/integration/test_corpus_retrieval.py` | NEW — end-to-end against curated corpus. |
| `CHANGELOG.md` | `[0.40.0]` entry. |

### What stays

| Module | Why |
|---|---|
| `src/lies/agents/query_synthesizer.py` | Unchanged contract; same prose-answer output shape. |
| `src/lies/agents/lint*.py`, `repair*.py` | Different surface; out of scope. |
| `src/lies/mcp/server.py::lint` tool | Different concern (health-check). |
| `src/lies/mcp/server.py::reindex` tool | qmd lifecycle; out of scope. |
| Wiki path: `src/lies/memory/*`, `src/lies/wiki/*` | Dormant code path per AGENTS.md; v0.40 doesn't touch wikis. `init_wiki` is removed from the MCP surface. |

## Decisions locked during brainstorming

1. **Scope:** Option C — full port of ask's design. Not just the retrieval-layer fix (option B).
2. **Phasing:** Single release. All of C ships in v0.40.0.
3. **Migration:** Hard cutover. No deprecation wrappers. Old tools removed in the same PR that ships the new ones.
4. **Naming:** Adopt ask's vocabulary minus the `core__` plugin-namespace prefix. Final tool list:
   - `collections_read`
   - `search`
   - `read`
   - `ask`
   - `lint` (kept)
   - `reindex` (kept)
   - `init_wiki` REMOVED (defer wiki concerns to post-v0.40)
5. **Test corpus:** Curated hand-written corpus, ~30 docs across 5 collections. ~20 query→expected_pages fixtures. Stored in repo under `tests/fixtures/library/collections/`.
6. **Librarian model:** Operator choice. No model-quality gate. Document that snippet-review quality is model-dependent.

## Architecture

```
┌──────────────┐       ┌─────────────────────────────────────────┐
│  MCP client  │──────▶│  collections_read   ── live registry   │
│  (Claude,    │       │  search / ask / read                   │
│   OpenCode)  │       └─────────────────────────────────────────┘
└──────────────┘                          │
                                         ▼
              ┌──────────────────────────────────────────────┐
              │  librarian_agent  (pydantic-ai subagent)     │
              │                                              │
              │  Step 1: Classify                            │
              │   collections_read("list")                   │
              │   → build tag_expr from registry              │
              │                                              │
              │  Step 2: Search                              │
              │   search(question, tag_expr, exclude_tags)   │
              │   → ranked hits + snippets                    │
              │                                              │
              │  Step 3: Read (SNIPPET-REVIEW decides)        │
              │   LLM reviews snippets, picks top-N paths     │
              │   read(picked_paths) → full bodies            │
              │                                              │
              │  Step 4: Return                              │
              │   package excerpts into LibrarianOutput       │
              └──────────────────────────────────────────────┘
                                         │
                                         ▼
              ┌──────────────────────────────────────────────┐
              │  ask(question) → SynthesizeEnvelope          │
              │    └─ search() → excerpt bundle                │
              │    └─ synthesizer LLM → answer + citations    │
              └──────────────────────────────────────────────┘
```

Three layers:

1. **Tools** — `collections_read`, `search`, `read`, `ask`. Plus `lint` and `reindex` (unchanged).
2. **Librarian agent** — pydantic-ai subagent with the 4-step prompt. Step 3 picks reads.
3. **Synthesizer** — unchanged contract, enriched envelope.

## Data flow

### `collections_read`

```
client → server → registry
  │──collections_read──▶│
  │                     │──read registry files──▶ ~/.local/share/lies/library/collections/<name>/config.yaml + frontmatter scan
  │                     │◀──registry rows───────
  │◀──registry rows─────│
```

### `search`

```
client → server → ground.py → qmd daemon
  │──search──▶│
  │           │──parse tag_expr──┐
  │           │──resolve tags────┤
  │           │   vs registry    │
  │           │                  │
  │           │──single POST /query──────────▶
  │           │   searches: vec+lex
  │           │   collections: [...]
  │           │   limit: 10
  │           │◀──ranked hits + snippets───────
  │           │
  │           │──return SearchResult
  │◀──────────│
```

Single round-trip to qmd. Hybrid vec+lex. Collections filtered. No per-collection fan-out. Library-wins-on-slug-conflict merge happens inside qmd.

### `read`

```
client → server → librarian → qmd / memory
  │──read────▶│  paths=[…]
  │           │──split paths by source:
  │           │   wiki (page-…) → memory
  │           │   library (coll/…) → qmd_get
  │           │◀──path → body dict─────────────
  │◀──────────│
```

Per-path dispatch. Failures: log + skip; if all fail, raise `ToolError`.

### `ask`

```
client → server → librarian agent → synthesizer
  │──ask──────────▶│
  │                │──librarian_agent.run(question, deps)──────▶│
  │                │                                             │
  │                │   Step 1: Classify                         │
  │                │     collections_read("list")                │
  │                │     → registry rows                         │
  │                │     → build tag_expr from matches           │
  │                │                                             │
  │                │   Step 2: Search                           │
  │                │     search(question, tag_expr, excl)        │
  │                │     → ranked hits + snippets                │
  │                │                                             │
  │                │   Step 3: Read (SNIPPET-REVIEW)             │
  │                │     LLM reviews snippets, picks top-N paths │
  │                │     read(picked_paths) → full bodies        │
  │                │                                             │
  │                │   Step 4: Return                           │
  │                │     package excerpts into LibrarianOutput   │
  │                │◀──LibrarianOutput───────────────────────────│
  │                │                                             │
  │                │──synthesizer_agent.run(librarian_output)──▶│
  │                │◀──prose answer + citations──────────────────│
  │                │                                             │
  │                │──SynthesizeEnvelope                        │
  │◀───────────────│
```

Step 3 is the structural fix. The LLM sees `opencode/v2/docs/build/plugins.md`'s snippet showing `Plugin.define({ id, setup(ctx) })` and ranks it above `discover-plugins.md`'s install-command snippet for a "compare authoring" question.

## Contracts

### `collections_read`

```python
def collections_read(
    subcommand: Literal["list", "tag_list", "info"],
    name: str | None = None,
) -> list[dict] | dict:
```

- `subcommand="list"` → list of `{"name": str, "tags": list[str], "scope_keywords": list[str]}` rows.
- `subcommand="tag_list"` → `dict[str, list[str]]` mapping tag → collections.
- `subcommand="info"` + `name` → single collection dict with full metadata.
- Empty registry → empty list / empty dict.
- Missing collection → `ToolError("collection not registered: <name>")`.

### `search`

```python
def search(
    question: str,
    tag_expr: str | None = None,
    exclude_tags: list[str] | None = None,
    hypothetical: str | None = None,
) -> SearchResult:
```

```python
@dataclass
class SearchResult:
    hit: SearchHit | None
    hits: list[SearchHit]
    unknown_tags: list[str]
    no_coverage: bool
    searched_scope: list[str]
```

- `tag_expr=None` → searches every registered collection.
- `tag_expr="c:foo|c:bar"` → searches the resolved set.
- `tag_expr="c:nope"` → `unknown_tags=["c:nope"]`, `no_coverage=False`, `searched_scope=[]`, no daemon call.
- `exclude_tags=["t:linux"]` → passed to librarian, which applies site-side.
- `hypothetical` → used as the dense-leg query instead of `question` (HyDE pattern from ask).

### `read`

```python
def read(paths: list[str]) -> dict[str, str]:
```

- `paths` is a list of wiki page IDs (`page-…`) and/or library paths (`<collection>/<path>`).
- Returns `{path: body}` for every successful read. Failed paths are dropped (logged at WARN).
- All reads fail → `ToolError("all reads failed")`.
- Empty `paths` → empty dict.

### `ask`

```python
def ask(
    question: str,
    tag_expr: str | None = None,
    exclude_tags: list[str] | None = None,
    file_back: bool = False,
) -> SynthesizeEnvelope:
```

```python
@dataclass
class SynthesizeEnvelope:
    question: str
    tag_expr: str | None
    answer: str
    citations: list[CitationSnippet]
    pages_read: list[str]
    fallback_used: bool
    synthesis_used: bool
    fallback_reason: str | None
    searched_scope: list[str]   # NEW (additive)
```

- `file_back=True` → `ToolError("file_back deferred until v0.41")` — same posture as today.
- `synthesis_used=False` → empty `answer`, `fallback_used=True`, `fallback_reason="<reason>"`.

## Failure modes

| Failure | Behavior |
|---|---|
| qmd daemon down | `search()` returns `hit=None, hits=[], no_coverage=True, fallback_reason="qmd unreachable"`. Synthesizer returns "No relevant content found in library." |
| Empty scope (resolved tags match 0 collections) | `search()` short-circuits with `searched_scope=[], no_coverage=False`. `ask()` returns "No collections matched the filter." |
| Unknown tag | `search()` returns `unknown_tags=["<spec>"]`. `ask()` envelope surfaces "Tag '<spec>' matched no collections." |
| Read failure (per-path) | Logged at WARN, path skipped. If ALL reads fail, falls back to snippet-only excerpts. |
| `file_back=True` | `ToolError("file_back deferred until v0.41")`. |
| Librarian LLM picks 0 paths in Step 3 | Fall back to snippet-only excerpts. Surface in `fallback_reason`. |

## Test corpus (`tests/fixtures/library/collections/`)

~30 hand-written docs across 5 collections:

| Collection | Docs | Topic |
|---|---|---|
| `alpha` | 8 docs | CLI plugin authoring (TypeScript, `Plugin.define({ id, setup })`) |
| `beta` | 6 docs | Plugin marketplace (JSON manifest, distribution, scopes) |
| `gamma` | 5 docs | LSP integration (per-language server config, `lsp` block schema) |
| `delta` | 6 docs | Eval-driven plugin testing (`plugin eval` workflow) |
| `epsilon` | 5 docs | Plugin hints (UX nudge mechanism, rate-limiting) |

Each doc ~50–150 lines. Frontmatter with `tags`, `scope_keywords`. Total ~2500 lines of curated content.

## Test fixtures

~20 query→expected_pages fixtures:

| Query | tag_expr | Expected top-3 hits (deterministic) |
|---|---|---|
| "How do I author a CLI plugin?" | `c:alpha` | `alpha/cli-plugin.md`, `alpha/plugin-anatomy.md`, `alpha/api/define.md` |
| "Compare plugin manifests across tools" | `c:alpha\|c:beta` | top-3 should include ≥1 from each collection (diversity floor) |
| "Set up pyright for Python" | `c:gamma` | `gamma/lsp-config.md`, `gamma/python-pyright.md`, `gamma/lsp-block.md` |
| "Test a plugin before release" | `c:delta` | `delta/eval-workflow.md`, `delta/grading.md`, `delta/ci-gate.md` |
| "What's a plugin hint?" | `c:epsilon` | `epsilon/hints.md`, `epsilon/rate-limit.md`, ... |

Each fixture asserts specific page names in `pages_read` (deterministic) AND specific excerpts in the synthesis (semantic — checked via citation presence).

## Unit-test surface (rewrite)

| File | Surface | Tests |
|---|---|---|
| `tests/unit/mcp/test_collections_read.py` | NEW | ~6 (list, tag_list, info, missing-collection, malformed-config, scope-keyword extraction) |
| `tests/unit/mcp/test_search.py` | REWRITE (was test_ground.py) | ~12 (single-batch payload shape, scope resolution, unknown_tags, no_coverage, hybrid vec+lex, qmd-down fallback) |
| `tests/unit/mcp/test_read.py` | NEW | ~5 (wiki paths, library paths, mixed dispatch, read failure, empty input) |
| `tests/unit/mcp/test_ask.py` | REWRITE (was test_synth.py) | ~10 (envelope shape, searched_scope propagation, fallback paths, file_back rejection) |
| `tests/unit/agents/test_librarian.py` | REWRITE (4-step pipeline) | ~15 (classify step, search step, snippet-review step with stub LLM, return shape, librarian LLM model resolution) |
| `tests/unit/test_grounding_resolution.py` | DELETE — replaced by test_search.py | 0 |
| `tests/unit/test_qmd_mcp_fallback.py` | KEEP | unchanged |
| `tests/integration/test_corpus_retrieval.py` | NEW | ~8 |

## Integration-test surface

End-to-end against the curated fixture corpus:

- `test_query_authoring_plugin_alpha` — `ask("How do I author a CLI plugin?", tag_expr="c:alpha")` returns envelope with `pages_read` containing `alpha/cli-plugin.md`.
- `test_query_or_diversity_floor` — `ask("Compare plugin manifests", tag_expr="c:alpha|c:beta")` returns hits from BOTH collections (≥1 each).
- `test_search_returns_searched_scope` — `search("anything", tag_expr="c:alpha")` returns `searched_scope=["alpha"]`.
- `test_search_unknown_tag_surfaces` — `search("anything", tag_expr="c:nope")` returns `unknown_tags=["c:nope"]`, `no_coverage=False`, `searched_scope=[]`.
- `test_ask_includes_librarian_searched_scope` — `ask(...)` envelope carries `searched_scope=[…]`.
- `test_read_dispatches_library_paths_to_qmd` — `read(["alpha/cli-plugin.md"])` returns body via qmd_get.
- `test_read_dispatches_wiki_ids_to_memory` — `read(["page-…"])` returns body via memory_service.
- `test_librarian_snippet_review_picks_authoring_over_install` — `ask("How do I author?", tag_expr="c:alpha|c:beta")` returns top-1 from `alpha/cli-plugin.md` even though `beta/install.md` ranks higher in qmd's BM25.

The last test is the structural pin — it asserts that the snippet-review step overrides qmd's blind ranking for the page most relevant to the user's intent.

## Versioning

**0.39.2 → 0.40.0.** New MCP tool surface is a breaking change. Align with project convention: minor bump despite the breaking surface (the F18 cutover did this — `v0.33.0` cut `wiki_search` for `wiki_search_v2` as a minor).

### Files touched for the version bump

| File | Change |
|---|---|
| `pyproject.toml` | `version = "0.40.0"` |
| `src/lies/__init__.py` | `__version__ = "0.40.0"` |
| `uv.lock` | auto-updated by `uv sync` |
| `CHANGELOG.md` | New `## [0.40.0]` section. Lists removed tools (`wiki_search`, `init_wiki`, `ask_question`, `ask_ground_question`), renamed tools (`wiki_read`→`read`, `wiki_catalog`→`collections_read`, `synthesize`→`ask`, `ground`→`search`), and the new 4-step librarian pipeline. Notes the new curated corpus under `tests/fixtures/library/collections/`. |
| `README.md` | Update MCP tool list. Drop the prompts section (no more `answer` / `cite` / `orient` etc. — all replaced by `ask` + `search` + `read`). |
| `docs/MCP_INSTRUCTIONS.md` | Rewrite for new tool surface. |
| `docs/ROUTING.md` | Update tag-filter dispatch examples. |
| `src/lies/mcp/instructions.md` | The MCP `instructions` payload. New tool list, new workflow. |
| `src/lies/mcp/prompts/*.md` | Delete prompt files (no more `answer`, `orient`, `ingest`, `lint`, `sync`, `file-back`, `cite` slash prompts — the librarian prompt moves into `librarian_agent`'s system prompt). |

## Migration risk

| Caller | Risk | Mitigation |
|---|---|---|
| External MCP clients (Claude Code, OpenCode) using `mcp__lies__wiki_search` | hard fail on every call | CHANGELOG headline: "BREAKING: 4 tools removed, 4 tools renamed". Update MCP `instructions` to list the new names. |
| Orchestrator / internal callers using `synthesize` / `ground` / `wiki_search` | internal compile error | Same PR migrates the orchestrator. No deprecation window. |
| Integration tests in `tests/integration/` referencing old tool names | tests fail | Same PR rewrites the tests. |
| Downstream MCP consumers (e.g. user scripts that invoke `mcp__lies__ground`) | hard fail | Document in CHANGELOG. No deprecation — operator chose hard-cutover. |

## Rollout

1. **Branch:** `librarian-v040` (sibling worktree, per global worktree rule).
2. **PR sequencing:** single PR. Title: `feat(mcp): port ask's retrieval pipeline (v0.40.0)`. Body lists the four removed, four added, breaking-change callout, link to this spec.
3. **Pre-commit gates:** `make check` (ruff + ty + format), `make unit-test`, commit-msg trailer ban, no `Co-Authored-By`.
4. **Merge target:** `main`.
5. **Tag:** `v0.40.0` after merge.
6. **Post-merge:** operator re-installs via `uv tool install --reinstall .`, restarts `lies mcp up`, verifies the new tool surface against the live library.

## Out of scope (deferred)

- **Wiki surface (`init_wiki` and friends).** Deferred to post-v0.40. `WikiMemoryService` stays in source, dormant.
- **`file_back=True` write-tool spec.** `ask()` raises `ToolError("file_back deferred until v0.41")` — same posture as today.
- **Librarian model-quality gate.** Operator-configured model is trusted. Quality-floor enforcement deferred.
- **Two-stage narrowing for `>10` collection tag sets.** Single-batch query handles the common case. Two-stage narrowing lives in ask but isn't needed for lies's typical 5–10 collection library.

## References

- Live-debug sessions: CC `db4bd25e-e72e-4eee-8c25-0645d37f82ef`, `fde8d304-c2e5-4e23-9bc3-a415d44d3b25`; OC `ses_f200e9482ffedIx3m2c8dUuikT`, `ses_f201c3a6fffe42K0RORkurO1uM`.
- ask repo: `/home/divinefilth/code/project-notes/ask/repo/ask/`
- Existing related work: PR #108 (sequential qmd fan-out), PR #110 (`lies sync` chains qmd update + embed), PR #111 (15s timeout).
