# AGENTS.md

Source of truth for agents working in the `lies` repository.

## Local-only Memory Files

@AGENTS.local.md

## Project context

**LIES** — Library of Inconsistent Explanations & Sources. A
`pydantic-ai`-harness agent maintains a git-backed wiki of interlinked
markdown over a corpus of raw sources. The human curates sources and
asks questions; the agent does bookkeeping.

This host runs **knowledge-collections-only**: no wikis are registered
under `~/.local/share/lies/<name>/`. Retrieval fans out across the
global library at `~/.local/share/lies/library/collections/<name>/`.
Wiki code paths (`WikiMemoryService`, `WikiIdentity`, page-author
agents) remain in source but are dormant.

## Pre-PR checklist

1. **Version bump per SemVer** across `pyproject.toml`,
   `src/lies/__init__.py`, `uv.lock`. Non-conventional rules:
   - The MCP **prompt** surface is host configuration, not API — breaking
     a prompt signature is **minor**; breaking a **tool** signature is major.
   - A tool response *field* is additive → minor, even when its semantics
     shift. Test: does a caller on the documented contract still work?
   - When only prompts break, still write `feat(mcp)!:` — the `!` records
     that a caller-visible signature changed.
2. **`CHANGELOG.md`** — an entry under the in-progress section
   (`test_the_declared_version_has_a_section` enforces it).
3. **`README.md`** — commands, config, install, behaviour changes.

## Pre-existing issues

Treat any pre-existing issue — on `main`, in the tracker, or in
TODO/FIXME/XXX — **as your own to solve**. Resolve or escalate; do not
dismiss it as out-of-scope.

## Source layout

```
src/lies/
├── agents/          # sub-agent prompt YAMLs (source-reader, page-writer,
│                    # linter, librarian, query-synthesizer)
├── capabilities/    # harness adapters (CodeMode, Memory, Planning)
├── cli/             # Typer CLI (init / ingest / query / lint / mcp / REPL)
│                    # ├── catalog.py  `lies catalog`
│                    # └── qmd.py      `lies qmd` operator group
├── config.py        # env-driven config
├── library/         # collection configs + deterministic mirrors
│                    # └── ingest.py   source/batch ingest, idempotency
├── mcp/             # FastMCP server; prompts, grounding, synth, daemon
│                    # ├── prompts_impl.py  7 slash prompts + _split_tail
│                    # ├── read.py          body retrieval
│                    # └── daemon.py        pidfile lifecycle for up/down/status
├── memory/          # invisible-memory layer (service, catalog, retrieval)
├── orchestrator.py  # top-level Orchestrator
├── qmd/             # qmd CLI + MCP adapters — read access.py FIRST
│                    # ├── access.py    THE SEAM: every call LIES makes to qmd
│                    # ├── lifecycle.py daemon status/up/down/recycle
│                    # └── lock.py      cross-process flock
├── markdown_spans.py # F37 span parser
├── query/           # index.md parser + synthesizer; citation.py
└── wiki/            # git + layout primitives
```

Tests mirror it: `tests/unit/`, `tests/integration/` (gated on
`INTEGRATION=1`), `tests/mcp/`, `tests/fixtures/`, `conftest.py`.

## The qmd access seam — `qmd/access.py`

One module owns every call LIES makes to qmd. **Read it before adding
any qmd call.** These are the rules; the measurements behind them live
in `CHANGELOG.md`.

### Transport

- **Routing is by capability, never availability.** `DAEMON_TOOLS` is
  what qmd's MCP server exposes (`query`, `get`, `multi_get`, `status`);
  `CLI_ONLY_OPS` is everything else — BM25 `search`, the mutating
  commands (`embed`, `update`, `cleanup`), the diagnostics (`doctor`,
  `ls`, `bench`, `collection`), and `mcp`. A down daemon raises
  `QmdDaemonUnavailable` naming `lies qmd up` and `LIES_QMD_URL`. **No
  degraded mode, no CLI fallback** — reporting an unreachable daemon as
  "no relevant content" is a claim about the corpus that is really a
  claim about the process.
- **`lies mcp up` is not the fix.** It starts LIES' own MCP server. The
  qmd daemon is `lies qmd up`.
- **`daemon_tool` returns the raw `CallToolResult`.** `get`/`multi_get`
  answer with a content block and `.data` is `None`. An *unknown
  collection* answers empty with **no error**.
- **Every qmd subprocess runs with `NO_COLOR=1`**, forced not inherited,
  so an operator's shell cannot change what LIES parses.
- **The `⠋ Gathering information` spinner is `ipull`, not qmd**, and only
  during a model download. It is not gated by `NO_COLOR`.

### Concurrency and the query gate

- **One MCP call at a time.** A search is ~10–30s and the daemon runs
  queries strictly one at a time, so `Promise.all` over `search` calls
  does not parallelise — it parks the last caller on a ~30s queue where
  it can exceed a client-side budget.
- **`query` single-flights; `get`/`multi_get`/`status` do not.**
  `daemon_tool` wraps `query` alone in `qmd_lock_async`, on the same
  site-wide `qmd.lock` the CLI helpers use, for the 120s budget
  `query_lock_wait_s()` reads from `LIES_QMD_QUERY_LOCK_WAIT`. The others
  answer in milliseconds and `read_library_bodies` issues one `get` per
  path, so gating them would park a whole batch behind the slowest query
  in the process.
- **Never widen the gate beyond `query`.** `if name == "query":` is
  narrow and must stay narrow: a gated call making a nested daemon call
  from a different context sees `_held_depth == 0` and blocks on a lock
  its own process holds. Measured, it self-deadlocks (0/10 at 90s). The
  gate is structurally immune because `query` makes no nested
  `daemon_tool` call and reads never take the lock.
- **The async gate is reentrant on one context — do not build the shape
  that reaches it.** `qmd_lock_async` snapshots a `_held_depth`
  ContextVar and contextvars propagate *into* `anyio.to_thread` workers,
  so a path that takes the lock then dispatches a `query` through
  `_run_blocking` runs the query with **no inode held**, silently. Not
  reachable today; re-verify before assuming that holds.
- **Awaiting in the gate buys thread tokens, not loop protection** — the
  `search` body runs via `anyio.to_thread` already. A blocking poll
  would park one of anyio's thread tokens for the length of the wait.
- **A tool-level "interrupted"/"aborted" result means the budget expired
  on queued work — not that the server is sick.** Read it as "this call
  took too long." The tell is a *negative*: abort plus **zero new lines
  in `~/.cache/qmd/mcp.log`** means no query was ever dispatched, so it
  queued and timed out rather than failing inside the daemon.
  **Recycle-on-abort is the wrong reflex** — it costs a model reload and
  hides the real cause.
- **`QmdLockBusy` is not a transport failure.** Nothing was dispatched,
  so there is no daemon state to recycle: **wait and retry, don't
  recycle.** The reachability probe sits *inside* the gate for the same
  reason — in front of it, a down daemon surfaces as `QmdLockBusy` and
  sends the operator to wait when the one that works is `lies qmd up`.
- **The gate wraps the recycle, so a wedge holds the inode for tens of
  seconds past its own deadline.** The path under the lock is
  `_call_daemon_tool` → `_call_with_recovery` → `_recycle_or_report` →
  `recycle_qmd_daemon(ready_timeout=30.0)` plus `_reap_qmd_daemon`'s grace
  sleeps and `_spawn_qmd_daemon`. A wedged query therefore blocks
  `lies qmd embed`/`update` host-wide. A deliberate trade — the inode is
  what makes the CUDA VMM reservation race unreachable — and **not a
  deadlock**: `qmd/daemon.py` takes no qmd lock and the spawn shells
  `qmd mcp --http --daemon` directly.

  Worst case: a measured **30 s floor and a 61 s ceiling**.

  | scenario | figure |
  | :--- | :--- |
  | prompt status, prompt spawn | 30 s (floor) |
  | status running, SIGTERM missed | 34 s |
  | prompt status, spawn hangs | 45 s |
  | `qmd status` hangs, prompt spawn | 45–59 s (a cliff, not noise) |
  | both hang | 61 s (ceiling) |

  Never quote a single figure: 45 s summed mutually exclusive component
  bounds, 80 s is not an upper bound at all. **The spawn has its own 15 s
  bound**, so "prompt status" does not mean prompt everything, and the
  spawn-hang row dominates the middle. The `qmd status` row is a **band**
  measured **44.9 to 59.4 s** by sweeping: a second in-loop `qmd status`
  call stops fitting the budget at the **0.1475** sweep boundary, clearing
  it by 0.25 s of headroom; one bound further back costs a whole
  `STATUS_TIMEOUT_S`. The band's spread is `time.sleep` jitter in the
  harness — `qmd status` was called 3 in 20 runs of 20, so the call count
  is stable and the spread is not. The sweep that derives the boundary
  brackets the step: 0.1450 takes two in-loop calls (59.4 s), 0.1470
  takes one (44.9 s). The reap's grace windows are **reachable in
  exactly one row** — a hanging `qmd status` reports `running=False` and
  forgoes them, which is why that row's cost is the ceiling and not a
  grace cost. The reader loop (`_subprocess._run_qmd`) must be real or
  the rescaling is inert. **Scaled:** `STATUS_TIMEOUT_S`,
  `ready_timeout`, `poll_interval`, and the reap's `grace`/`poll` —
  except `_subprocess._POLL_INTERVAL_S`, which is neither a timeout nor
  a sleep but the loop's `sel.select()` granularity and so **does not
  scale by itself**; leaving it unscaled puts the reported figure 40%
  low. The `qmd status` row is a cliff, not noise: "unstable" invites a
  reader to average it away, and 0.25 s of headroom is the whole
  difference between the two ends.
- **There is no log line on the wait path, and don't add one expecting
  it to be read.** The wait never reaches the daemon (it's on a local
  inode); the heartbeat is an in-process timer, not a write.
- **A release removes the triad it acquired, not what the environment
  names now.** Each lock-acquire side must release the same three files
  it acquired, or `LIES_QMD_LOCK_PATH` moving mid-acquisition silently
  leaks a heartbeat that the *next* acquire reads as staleness.
  `tests/unit/qmd/test_lock_release.py` holds both halves. `SIGKILL` is
  the one exit nothing bounds.
- **Idle bounds.** Queries use 30s; `embed`/`update` are silent their
  whole duration so they pass `idle_timeout = total * 0.5`. When adding a
  qmd command, ask whether it talks while it works.

### Clients

- **`_daemon_client` builds a fresh `Client` per call. Do NOT "optimise"
  it back into a cache.** One `Client` is one MCP session, and one
  session cannot serve concurrent calls. The daemon is machine-global,
  so a cached client is shared by every LIES session on the host.
- **Across event loops a shared client breaks under concurrency**, and no
  per-call deadline bounds it — `timeout=` expires but the call does not
  return. A client shared *within* one loop survives concurrent fast
  calls; do not carry that across the loop boundary. Treat it as a
  hazard to design around, not a proven root cause.
- **`_build_qmd_httpx_client` takes `**kwargs`** — fastmcp calls it with
  `follow_redirects=`; a fixed signature fails at connect.
- **The transport does not time out idle streams**, so there is no
  server-side keepalive to add. FastMCP's `PingMiddleware` is
  deliberately not registered — it was added for a failure mode that
  does not exist.

### Failure taxonomy

- **Three daemon failure modes, three answers.** A *wedge* recycles and
  raises `QmdDaemonWedged` with a `mcp.log` tail; no transparent retry
  (a fresh daemon re-wedges on the same payload). *Unreachable* recycles
  and retries once. A *protocol-level rejection* is not a transport
  failure and re-raises unchanged. `classify_call_error` is the only
  place that decides.
- **A retry's failure is classified, not assumed** — after a recycle, an
  unowned reason propagates, a re-wedge raises `QmdDaemonWedged`, only
  still-unreachable raises `QmdDaemonUnavailable`.
- **Deliberate taxonomy gap.** `HTTPStatusError` (5xx) and
  `LocalProtocolError` classify as `passthrough` (no recycle);
  `RemoteProtocolError` keeps the recycle-retry path. Pinned in
  `tests/unit/qmd/test_access.py`.
- **Match exception class *names*, not httpx types.** fastmcp vendors its
  own httpx (`httpx2`); a dead session arrives as a bare `RuntimeError`
  with the real error on `__cause__`, which matches neither an
  `httpx.HTTPError` nor `OSError`. **Code against the wrapper, not the
  vendored module name.** Re-derive by probe before changing the matcher.
- **The recycle probe must catch a bare `RuntimeError`** — a stopped
  daemon raises it (fastmcp wraps connect failures that way), and
  missing it made `_recycle_or_report` report a failed restart while the
  daemon was actually coming up.
- **`last_output` must come from the daemon that actually wedged.** A
  tail attributed to the wrong daemon is worse than none.

### Addressing the daemon

- **The default URL carries `/mcp`** — a bare origin reaches a live daemon
  and 404s, which reads as a down daemon.
- **qmd binds a NAME, not an address — the literal `localhost`.** `--port`
  selects the port only; nothing selects the address family, and Node
  takes whichever `/etc/hosts` lists first. On an IPv6-first host that is
  `[::1]` and an IPv4 dial is refused. **Probe the name qmd bound, and do
  not pin your probe's family** — `socket.create_connection`, not
  `socket.socket(AF_INET, …)`, which filters name resolution to v4 and
  reproduces the exact refusal the name fixes. Pinned by
  `tests/unit/qmd/test_daemon_host.py`. The settling diagnostic is qmd's
  own `mcp.log`, which logs the URL it actually reached — a "nothing is
  listening" report that never reads it has not distinguished *did not
  start* from *started elsewhere*.
- **Every reap-then-spawn threads the port from the URL it will probe**
  (`daemon.daemon_port_from_url`) — six call sites across four locations:
  `capability.py`, `access.py`, `recycle_qmd_daemon`, and three paths in
  `daemon.py`'s `ensure_qmd_daemon`. `test_agents_line_refs.py` pins the set
  exhaustively by content so a new site inherits the pin.
- **`mcp.pid` is keyed to the cache root, NOT the port.** A second daemon
  on a different port but the same cache root overwrites the machine
  daemon's pidfile, and both `qmd_daemon_state` and `_reap_qmd_daemon`
  read it — so the reap can SIGTERM the wrong process.
- **`qmd mcp --http --daemon` has `--port` and has no `--data-dir`.**
  `XDG_CACHE_HOME` is the lever; it relocates `index.sqlite`, `mcp.pid`
  **and** `mcp.log` together (appends its own `qmd/` segment).
- **The qmd index is keyed by `$XDG_CACHE_HOME`, not `$XDG_DATA_HOME`.**
  Sandboxing the library root alone moves the mirrors and nothing else.
- **The sidecar records a "scope", and a scope is not an index.** All
  call sites pass `daemon._canonical_scope()` so `check_scope_match`
  compares like with like; it does not select the daemon's index.
- **`validate_scope(scope)` is the shared pre-check.** Without it, one
  unresolvable name silently returns zero rows.

### The last hop to the reader — deterministic

- **Do not make the last hop model-mediated.** The librarian is an LLM
  and `LibrarianOutput` is its `output_type`, so its `transient` /
  `fallback_reason` are *claims*. The tool closures record every
  envelope into a per-dispatch `SearchDiagnostics`; the model copying a
  string is not the delivery mechanism.
- **The oracle is hits ∪ reads, not the envelope's hits** — `read`
  accepts any resolvable path, so link-following produces evidence the
  envelope never named. `read` keys beginning with `_` are skipped
  (`_missing` is synthetic).
- **`with_search_diagnostics` drops any excerpt whose `slug` is not
  attested.** A total drop yields `transient=True, no_coverage=False` so
  the reader learns the librarian invented the bundle rather than that
  the library has nothing.
- **Two bounds, both pinned:** an all-attested bundle is returned
  untouched, and a recorded failure is **sticky** across a later clean
  search (the budget was already spent queueing).
- **Fabrication is closed at the path level; the text level is a stated
  limit.** A model that reads a page and attributes a passage not in it
  still passes. `test_a_span_body_is_not_checked` asserts the limit so it
  can't be forgotten.
- **All three dispatch sites carry a record, by two mechanisms.** Two
  thread a `SearchDiagnostics` argument; the orchestrator binds to the
  *dispatch* via a `ContextVar` (`search_diagnostics_scope`) because it
  registers tools once for the agent's lifetime. The record is a
  keyword-only argument (not a global) because the MCP server serves
  overlapping calls. `tests/unit/test_librarian_dispatch_record.py`
  holds it.
- **`Citation.source` on `lib_ask` is model-asserted, not verified.** A
  path the librarian never handed over still becomes a `Citation`. A
  caller that needs it real must resolve it through `read`.

## The read tool's bodies — `mcp/read.py`

- **The body is the document and nothing else**, so the library branch
  issues `get` with `lineNumbers: false`.
- **One `get` per path, never `multi_get` for a batch** — `multi_get`
  *skips* files over its 10 KB default and collapses on one unresolvable
  entry.
- **A notice is not a body.** A result with no resource block raises
  rather than returning `""`.
- **Who owns the failure decides whether it is skippable.** A down or
  wedged daemon re-raises — swallowing it turns a reachable failure into
  a claim about the corpus.
- **Both spellings of "no body" are skipped together** (call raises, or
  succeeds with notices only); don't branch on which channel qmd used.
- **Partial batches carry unresolved paths under the synthetic key
  `"_missing"`**.
- **The sync bridge runs its own loop when one is already running** —
  `asyncio.run` from a threaded caller raises.

## `ground()` is public API — its diagnostics are NOT dead code

`src/lies/mcp/__init__.py` declares `__all__ = ["ground", "mcp"]` and
re-exports `ground` "for in-process callers". That is a **supported
surface, not an implementation detail.** A whole-branch review flagged
`ArchivistDigest.fallback_reason` and the `except QmdLockBusy` arms as
unreachable because `ground()`'s only in-tree caller is `synthesize()`
(which tests call). That is true of the **call graph** and false of the
**contract** — an in-process caller reaches `ground()` and every
diagnostic on it. **Do not propose these as dead code.** Two mechanisms,
two shapes, both live: the MCP tool path carries the facts via
`SearchDiagnostics`; `ground()` reaches `daemon_tool` directly, so the
exception arrives as a real exception and its own arms catch it
(`grounding.py:465` and `:535`).

## Prompt surface

- **Route user questions through prompts, not `lib_ask`.** Call
  `get_prompt(name="ask" | "ground", arguments={"tail": ...})`.
- **Every prompt takes exactly one `str`, named `tail`.** A typed
  `bool`/`int`/`list[str]` past position one gets a bare word from the
  slash tokenizer and fails JSON decode. The slash path is single-token;
  `get_prompt` is the path for multi-word questions.
- **A surviving parameter name is a silent break, not a compatible one.**
  FastMCP filters `get_prompt` arguments to the declared signature, so a
  retired name means a retired call *succeeds* and drops its options.
- **`prompts_impl._split_tail` splits on whitespace only**, never a shell
  lexer, so an apostrophe in English doesn't raise.
- **Every prompt declares its own `value_flags` / `multi_word_flags` /
  `known_flags`**, and `test_prompts_flag_vocabulary.py` checks each
  advertised flag survives the parse.
- **Bodies read flags via `TailParse.flag_on()`** (both booleans and
  values) and `repeats_of()`.
- **`_refuse_unless_clean` is the one guard; all seven bodies call it** —
  it refuses on a missing or repurposed flag value, both of which make
  the rendered command wrong.
- **`+tag` / `-tag` atoms are read from the leading run only** —
  otherwise a question *about* option flags has its words eaten. `ground`
  parses flags from the leading run only; `ask` parses none at all.

## Data shapes

- **`Span`** (`markdown_spans.py`): `(heading_path, body, code_fence,
  start_line)`; `code_fence` marks spans inside fenced blocks.
  `PageRead.spans` is populated at read time.
- **`Citation.heading_path: list[str] | None`** (default `None`).
  `ClaimCitation.quote` is validated as a substring of the cited span.
- **`ArchivistDigest`** — `no_coverage` is true only when the dispatch
  *succeeds* and returns zero hits; `transient` marks *dispatch*
  failures, so a caller can tell "the daemon failed" from "the corpus has
  nothing".

All forward-only; additive fields default to safe sentinels.

## Dual-source routing

Library collections are primary; wikis are secondary. On slug collision
the library hit replaces the wiki hit entirely. Wiki-only hits render
with a `[secondary] ` marker.

## Ingest invariants

`_process_item` compares the incoming `source_hash` against the mirror's
frontmatter. **Three states, not two:** matching hash skips, differing
hash conflicts, and *no hash* is neither (reported as
`mirror-unmanaged:<slug>:no-source-hash`). A **self-ingest** — `--batch`
pointed at a collection directory — is the degenerate case of that third
state, refused outright (`SelfIngestRefused`, exit 2), checked by
containment not equality.

## Page-type conventions

Per-type required `## <Heading>` sections are declared in
`src/lies/schema/default_schema.md`. Lint surfaces
`missing_required_section` (`safe_to_fix=False`); the writer refuses
writes omitting them. Match is literal-substring — `## Evidence` matches,
`### Evidence` does not.

## Known flakes

Measured failures that are real and not a LIES bug. Don't re-derive or
retry a mitigation already measured and rejected.

- **`qmd embed` aborts on the CUDA VMM reservation** (WSL2):
  `cuMemAddressReserve(&pool_addr, CUDA_POOL_VMM_MAX_SIZE, …)` →
  `CUDA error: out of memory` → `ggml_abort`. **The label is false** — it
  reserves 32 GB of *virtual address space*, and peak usage is ~4.7 GB on
  a 24 GB card. `GGML_CUDA_NO_VMM` is compile-time. Use
  `make qmd-backend-fix` / `-check`. Two traps: the rebuilt file is not
  the loaded one (read the path from the backtrace), and a successful
  `dlopen` is no evidence a library works. A `bun install` reverts it.
  Ruled out by measurement: VRAM pressure, cross-process contention,
  and retry. Reproduces only through the tag-filter suite — a false
  all-clear on a simpler harness means nothing.
- **`INTEGRATION=1 pytest` is a different suite than plain `pytest`** —
  a plain run reports it skipped and exits 0; that green is not evidence.
  `test_tag_filter_end_to_end.py` intermittently fails with
  `QmdCommandError: … need embeddings`, **a different subset each run** —
  a varying subset is the diagnosis (a logic bug fails the same tests
  every time).
- **The suite's skips depend on the environment, not just the corpus.**
  Two gates that once read `qmd_daemon_reachable` mean different things:
  *is a daemon answering* (any process on the port) vs *may qmd run its
  LLM* (qmd reads `CI` at import and refuses behind it). Gating a real
  query on the first let a stray daemon decide the run. The corpus gate
  asks whether the daemon serves a corpus; the query gate asks whether
  LLM ops are disabled. Neither is implied by the other.
- **`store_collections` can be emptied by qmd itself** — a config
  momentarily declaring zero collections empties the table and the hash
  written for it then matches, making it self-perpetuating. Retrieval
  stays healthy, so it's hard to spot. Check the `collections:` block in
  `~/.config/qmd/index.yml` first. `registry_divergence` names the event.
- **A stopped qmd daemon is diagnosable only if the log survives** — qmd
  truncates `mcp.log` on every start and LIES recycles routinely. `_down`
  preserves it to `mcp.log.<stamp>` (keeping 5) before stopping.

## Quality gates

`make check` = lint + typecheck + format + unit tests; `make test` adds
integration. Pre-commit wraps the same targets. The **CI gate** is
`INTEGRATION=1 CI=true pytest tests/ --runslow` — plain `pytest` hides
the entire slow surface.

The per-test budget gate fails a non-slow test whose call phase exceeds
0.15s — but only after re-running in isolation, keeping the **minimum of
up to 3 passes** (one draw from a noisy distribution is not a
measurement). A breach that clears is reported as noise and the run
passes. Reaching for `@pytest.mark.slow` to silence a gate failure
removes the test from the default run rather than fixing anything.

## References

- Project overview: `README.md`
- Makefile targets: `Makefile`
- Release notes: `CHANGELOG.md`