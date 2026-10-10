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

1. **Version bump per SemVer.** Surfaces: `pyproject.toml`,
   `src/lies/__init__.py`, `uv.lock`. Three rules that are not
   conventional:
   - **The MCP prompt surface is host configuration.** The seven
     `@mcp.prompt` registrations are configuration a host reads to
     render a slash menu, not the programmatic API. Breaking change to
     a *prompt signature* takes **minor**; to a *tool signature*
     takes major.
   - **A tool response *field* is not a tool signature.** Adding a field
     to a response envelope is additive → minor, even when an existing
     field's semantics shift (0.47.0 added `search.transient` and
     stopped setting a false `no_coverage`). The test: does a caller
     following the *documented* contract still work? Yes → minor.
   - **The `!` marker does not follow the segment.** When only prompts
     break, write `feat(mcp)!:` anyway — the marker records that a
     caller-visible signature changed, and the version segment records
     how seriously the project takes it. A bare `feat(mcp):` hides the
     break from `make release`.
2. **`CHANGELOG.md`** — an entry under the in-progress section.
   `test_the_declared_version_has_a_section` requires the version to
   have a dated heading or be named in the `[Unreleased]` body.
3. **`README.md`** — commands, config, install, behaviour changes.
4. **`docs/`** — only if the change is reflected there.

## Pre-existing issues

Treat any pre-existing issue — one already on `main`, in the tracker,
or in TODO/FIXME/XXX — **as your own to solve**. Do not dismiss it as
out-of-scope or someone else's. Resolve or escalate.

## Source layout

```
src/lies/
├── agents/          # sub-agent prompt YAMLs (source-reader, page-writer,
│                    # linter, librarian, query-synthesizer). No indexer —
│                    # the catalog is deterministic.
├── capabilities/    # harness adapters (CodeMode, Memory, Planning, ...)
│   └── memory.py    # per-wiki namespace via WikiIdentity
├── cli/             # Typer CLI (init / ingest / query / lint / mcp / REPL)
│   ├── catalog.py   # `lies catalog` group
│   └── qmd.py       # `lies qmd` operator group
├── config.py        # env-driven config
├── library/         # collection configs + deterministic mirrors
│   ├── record.py, schema.py, config_io.py, bootstrap.py
│   ├── collections_cli.py   # `lies library` sub-app
│   └── ingest.py    # source/batch ingest, idempotency, quarantine
├── mcp/             # FastMCP server; prompts, grounding, synth, daemon
│   ├── prompts_impl.py  # 7 slash prompts + _split_tail
│   ├── read.py      # body retrieval
│   ├── grounding.py # ArchivistDigest, F19 citation contract
│   └── daemon.py    # pidfile lifecycle for `lies mcp up/down/status`
├── memory/          # invisible-memory layer
│   ├── service.py   # WikiMemoryService — sole owner of wiki mutation
│   ├── catalog.py   # sqlite catalog at <wiki>/.lies/catalog.db
│   └── retrieval.py # PageRead, spans
├── orchestrator.py  # top-level Orchestrator
├── qmd/             # qmd CLI + MCP adapters — read access.py FIRST
│   ├── access.py    # THE SEAM: every call LIES makes to qmd
│   ├── lifecycle.py # daemon status/up/down/recycle
│   ├── cli.py       # CLI wrappers for the qmd binary
│   └── lock.py      # cross-process flock
├── markdown_spans.py # F37 span parser
├── query/           # index.md parser + synthesizer; citation.py
├── schema/          # default schema + loader
├── utils/           # logging, shell helpers, exclusive.py
└── wiki/            # git + layout primitives
```

Tests mirror it: `tests/unit/`, `tests/integration/` (gated on
`INTEGRATION=1`), `tests/mcp/`, `tests/fixtures/`, `conftest.py`.

## The qmd access seam — `qmd/access.py`

One module owns every call LIES makes to qmd and encodes which
transport serves which operation. **Read it before adding any qmd
call.**

- **Routing is by capability, never availability.** `DAEMON_TOOLS` is
  what qmd's MCP server exposes (`query`, `get`, `multi_get`, `status`);
  `CLI_ONLY_OPS` is everything else including BM25 `search`, plus the
  diagnostic surface (`doctor`, `ls`, `bench`, `cleanup`, `collection`,
  `mcp`). A down daemon raises `QmdDaemonUnavailable` naming
  `lies qmd up` and `LIES_QMD_URL`. **No degraded mode, no CLI
  fallback** — reporting an unreachable daemon as "no relevant content"
  is a claim about the corpus that is really a claim about the process.
- **`lies mcp up` is not the fix.** It starts LIES' own MCP server. The
  qmd daemon is `lies qmd up`.
- **One MCP call at a time. A search is ~10–30s, and fanning out
  multiplies that instead of hiding it.** The qmd daemon executes
  queries strictly one at a time: three concurrent queries measured a
  **0.99x speedup** (29.48s vs 29.18s serial — the second waited for
  the first, the third for both). So `Promise.all` over `search` calls
  does not parallelise anything; it just parks the last caller on a
  ~30s queue where it can exceed a client-side call budget.

  **A tool-level "interrupted"/"aborted" result on a LIES call means
  the budget expired on queued work — it is not evidence about the
  server.** Read it as "this call took too long", not "the daemon is
  wedged" and not "the stream dropped". The evidence that settles it is
  in the logs, and it is a *negative*: an abort accompanied by **zero
  new lines in `~/.cache/qmd/mcp.log`** means no query was ever
  dispatched, so the call queued and timed out rather than failing
  inside the daemon. Recycle-on-abort is the wrong reflex here — it
  costs a model reload and hides the real cause.
- **The `query` path single-flights; `get`/`multi_get`/`status` do
  not.** `daemon_tool` wraps `query` alone in `qmd_lock_async`, on the
  same site-wide `qmd.lock` the CLI helpers use, for the 120 s budget
  `query_lock_wait_s()` reads from `LIES_QMD_QUERY_LOCK_WAIT` — per
  call, so a host can retune without a restart, and anything that is
  not a positive finite number (including `inf`) falls back to 120
  rather than raising on the retrieval path. The wait the daemon was
  already imposing now has an owner, a deadline and a heartbeat;
  exhaustion raises `QmdLockBusy` carrying the lock path, the
  holder PID, the elapsed wait and the budget — though on the ask path
  those facts reach the user only if the librarian mirrors the
  envelope's `fallback_reason`, and a non-compliant model degrades to a
  generic reason with no corpus claim. The other three answer in
  milliseconds and `read_library_bodies` issues one `get` per path,
  so gating them would park a whole batch behind the slowest query in
  the process — the queue they would join is one long query, not each
  other.

  **There is no log line on the wait path, and do not add one
  expecting it to be read.** Measured 2026-10-10 against the live
  daemon: a `search` blocked for 2 s added 12 lines to `qmd/mcp.log`,
  all transport plumbing, zero mentioning the lock — the wait never
  reaches the daemon, because the caller is waiting on a local inode.
  `_acquire_with_poll_async` writes nothing while it polls, and the
  heartbeat is an in-process timer, not a write. The evidence that
  survives is the ephemeral `.pid` sidecar beside the lock and the
  `fallback_reason` on the envelope.

  **Widening the gate to every daemon op self-deadlocks — measured.**
  With all four tools gated, mixed 5-search + 5-read load gave **0/10**,
  every call timing out at 90 s, the log naming the *daemon's own* PID
  as holder for the full 120 s budget before `QmdLockBusy`, while qmd
  kept answering in 119 ms. A gated call making a nested daemon call
  from a different context sees `_held_depth == 0` and blocks on a lock
  its own process holds. `if name == "query":` is narrow *and must stay
  narrow*; it is structurally immune because `query` makes no nested
  `daemon_tool` calls (the probe is the separate
  `qmd_daemon_reachable()`) and reads never take the lock.
- **`_daemon_client` builds a fresh `Client` per call. Do NOT
  "optimise" it back into a cache.** The requirement is that **no two
  calls share a client** — not that a client be exclusively owned while
  in use. A cached `Client` is one object with one MCP session behind
  it, and one session cannot serve concurrent calls. The daemon is
  machine-global, so that object is shared by every LIES session on the
  host.

  **The teardown story is false — do not reason from it.** It was
  claimed here that "the first exit tears down state the second still
  holds". That is wrong for the installed fastmcp 4.0.10:
  `Client._disconnect` decrements `nesting_counter` and returns while
  it is still positive (`fastmcp/client/client.py:1080-1084`), so an
  exiting context does not stop a session a sibling is inside.
  Refcounted teardown is not the mechanism, and that behaviour is
  fastmcp-version-specific — read the installed file before relying on
  it.

  What the probes support, and nothing stronger: **concurrent calls on
  one shared session can hang** (2 concurrent `query` gave
  `MCPError: Connection closed`; 3 gave a hang then
  `BrokenResourceError`). **Fast calls did not reproduce it** — 3
  concurrent `status` were all OK at ~0.1 s, 3 concurrent `get` all OK,
  and 1 `query` + 3 `get` concurrently all OK — so concurrency alone is
  not the trigger. Duration as the discriminator is an inference from
  those cases, not a threshold a probe isolated.

  And **across event loops, a shared client breaks under concurrency**.
  Measured 2026-10-10 against the live daemon and **transcribed, not
  re-measured at commit time** (the whole-branch review reproduced this
  independently before it was written here): 3 concurrent `status` calls,
  each on its **own** `asyncio.run` loop — fresh client per call gives
  **3/3 in 0.21 s**; one shared client gives **0/3**, still unfinished at
  a 30 s box *with the 20 s per-call timeout already expired* (the
  reviewer's run went past 300 s). Two things follow, and both correct
  claims made elsewhere in this section:

  - **Call duration is irrelevant here.** These are `status` calls that
    complete in ~0.2 s. "Fast calls did not reproduce it" holds only for
    a client shared *within* one loop; across loops even a 0.2 s call
    hangs. Do not carry the fast-call result across the loop boundary.
  - **No per-call deadline bounds it.** `timeout=` reaches `call_tool`
    and expires; the call does not return.

  Control, so the mechanism is not overstated: the same shared client
  driven by three loops **one at a time** returns 3/3 ok. So mere
  cross-loop *reuse* is fine and the failure needs cross-loop
  *concurrency*. The structural hazard is real and cited above — an
  `anyio.Lock` binds its backend at construction, so a client built on
  loop A carries an A-bound lock — but that alone does not explain a
  sequential cross-loop call succeeding, so treat it as a hazard to
  design around rather than a proven cause of the hang.

  Headline, measured 2026-10-10 against the live daemon and
  **transcribed, not re-measured at commit time**: 3 concurrent `search`
  calls gave **0/3 complete at 60.1 s** cached and **3/3 in 1.4 s**
  fresh, and the fixed client was clean at N=25 (8.3 s) and on mixed
  5-search + 5-read load (10/10, 2.53 s). Provenance matters more than
  usual here: this unreleased section has already retracted two
  measurements.

  The two claims that justified the cache were both false, which is why
  it looks free: the model does **not** get re-warmed (it lives in the
  qmd daemon, RSS flat at 1957-1965 MiB across every run), and the MCP
  handshake was **already** per-call because `_call` opened a session
  each call. Rebuilding costs +0.9 ms / +1 % on 20 `status` calls
  (median 69.1 ms shared, 70.0 ms fresh), 0 MiB VRAM delta over 5 runs.
  `tests/unit/qmd/test_access.py::test_concurrent_calls_never_share_a_client`
  pins it, and it stubs `fastmcp.Client` rather than `_daemon_client` —
  stubbing the function that holds the cache would leave it green
  against the exact defect it exists to catch.

  **`QmdLockBusy` is not a transport failure.** Nothing was dispatched,
  so there is no daemon state to recycle and no `mcp.log` line to
  read: **recycle is the wrong response, wait and retry is the right
  one.** The reachability probe sits *inside* the gate for the same
  reason — in front of it, a down daemon would surface as
  `QmdLockBusy` and send the operator to wait when the one that works
  is `lies qmd up`.

  **The gate wraps the recycle, so a wedge holds the inode for tens of
  seconds past its own deadline.** The path under the lock is
  `_call_daemon_tool` → `_call_with_recovery` → `_recycle_or_report` →
  `recycle_qmd_daemon(ready_timeout=30.0)`, plus `_reap_qmd_daemon`'s
  up to 4 s of `time.sleep` and `_spawn_qmd_daemon`. A wedged query
  therefore blocks `lies qmd embed`/`update` host-wide for tens of
  seconds after its own 60 s read deadline. That is a deliberate trade
  — the inode is what makes the CUDA VMM reservation race unreachable
  — and **it is not a deadlock**: `qmd/daemon.py` takes no qmd lock,
  and the spawn shells `qmd mcp --http --daemon` directly, so the
  recycle never waits on the lock it is holding.
- **The streamable-HTTP transport does not time out idle streams.** A
  GET stream held open for 30 minutes survived intact, carrying one
  ping and then 29+ minutes of silence. There is no server-side idle
  keepalive to add, and none is needed. FastMCP's `PingMiddleware` is
  deliberately **not** registered: it was added for a failure mode that
  does not exist, then removed once the hold falsified the premise.
- **`lies mcp down` disconnects every client on the host.** The LIES
  daemon is machine-global, so one session's `down` drops all of them
  at once — and that *is* a real transport failure, visible as
  `SSE stream disconnected` then `ConnectionRefused` in opencode's log.
  `down` refuses while the daemon is serving; pass `--force` when you
  mean it.
- **Three daemon failure modes, three answers.** A *wedge* recycles and
  raises `QmdWedged` carrying a `mcp.log` tail; no transparent retry,
  because a fresh daemon re-wedges on the same payload. *Unreachable*
  recycles and retries once. A *protocol-level rejection* is not a
  transport failure and re-raises unchanged. `classify_call_error` is
  the only place that decides.
- **`last_output` must come from the daemon that actually wedged.** The
  two call sites read on opposite sides of their recycle and both are
  correct. A tail attributed to the wrong daemon is worse than none.
- **A retry's failure is classified, not assumed.** After a recycle,
  take one of three routes: an unowned reason propagates, a re-wedge
  raises `QmdWedged`, only still-unreachable raises
  `QmdUnavailable`. Collapsing them tells the operator to start a
  daemon that is already running.
- **Deliberate taxonomy gap.** `HTTPStatusError` (5xx) and
  `LocalProtocolError` (client-side malformed) classify as
  `passthrough` — no recycle. `RemoteProtocolError` keeps the
  recycle-retry path. All three are pinned in `tests/unit/qmd/test_access.py`.
- **The taxonomy matches exception class *names*, not httpx types.**
  fastmcp vendors its own httpx as `httpx2`; a dead session arrives as a
  bare `RuntimeError` with the real error on `__cause__`. A taxonomy
  written against the httpx LIES declares matches none of them. Re-derive
  by probe before changing the matcher.
- **`_build_qmd_httpx_client` takes `**kwargs`** — fastmcp calls it with
  `follow_redirects=`; a fixed signature fails at connect. It builds a
  fresh `AsyncClient` per call and caches nothing. (An earlier version
  of this bullet described a cached client keyed on URL, and argued it
  was safe because an httpx *client* is not bound to a loop the way an
  MCP *session* is. That cache is gone — see the fresh-client bullet
  above — so the distinction no longer buys anything here. The `**kwargs`
  requirement is the part that still bites.)
- **`daemon_tool` returns the raw `CallToolResult`.** `get`/`multi_get`
  answer with a content block and `.data` is `None`. The daemon also
  answers an *unknown collection* with an empty result and **no error**.
- **`validate_scope(scope)` is the shared pre-check.** It reads the
  daemon's `status` and splits the input into served + absent. Without
  it, one unresolvable name silently returns zero rows.
- **The default URL carries `/mcp`** — a bare origin reaches a live
  daemon and 404s, which reads as a down daemon.
- **The qmd index is keyed by `$XDG_CACHE_HOME`, not `$XDG_DATA_HOME`.**
  qmd resolves it from the cache root, so redirecting the library root
  alone sandboxes the mirror files and nothing else. The qmd write
  helpers refuse that combination and name both variables; reads are
  unguarded, because the daemon is machine-global.
- **Every qmd subprocess runs with `NO_COLOR=1`**, forced rather than
  inherited, so an operator's shell cannot change what LIES parses.
- **The `⠋ Gathering information` spinner is `ipull`, not qmd**, and
  only during a model download. It is not gated by `NO_COLOR`; the lever
  is a warm model cache.
- **Idle bounds.** Queries use the default 30s; `embed` and `update` are
  silent for their whole duration, so they pass
  `idle_timeout = total * 0.5`. It is not 1.0 because the reader checks
  the total bound first, so an idle bound equal to the total can never
  fire and the `last_output` diagnostic is lost. **When adding a qmd
  command, ask whether it talks while it works.**

## The read tool's bodies — `mcp/read.py`

- **The body is the document and nothing else**, so the library branch
  issues `get` with `lineNumbers: false`.
- **One `get` per path, never `multi_get` for a batch** — `multi_get`
  *skips* files over its 10 KB default (1854 of 5987 docs here) and
  collapses on one unresolvable entry.
- **A notice is not a body.** `_resource_texts` and `_notices` keep them
  apart; a result with no resource block raises rather than returning `""`.
- **Who owns the failure decides whether it is skippable.** A down or
  wedged daemon re-raises — swallowing it turns a reachable failure into
  a claim about the corpus.
- **Both spellings of "no body" are skipped together** (the call raises,
  or succeeds with notices only). Do not branch on which channel qmd
  used; a batch's outcome must not depend on it.
- **Partial batches carry unresolved paths on the wire** under the
  synthetic key `"_missing"`.
- **The sync bridge runs its own loop when one is already running** —
  `asyncio.run` from a threaded caller raises, which is the bug
  `ground()` shipped with in #106.

## Prompt surface

- **Route user questions through prompts, not `lib_ask`.** `lib_ask` is
  the synthesizer inside the `ask` prompt's body. Agents should call
  `get_prompt(name="ask" | "ground", arguments={"tail": ...})`.
- **Every prompt takes exactly one `str`, named `tail`.** A typed
  `bool`/`int`/`list[str]` past position one receives a bare word from
  the slash tokenizer and fails JSON decode. Hosts bind one token and
  drop the rest, so the slash path is single-token only; `get_prompt` is
  the path for multi-word questions.
- **A surviving parameter name is a silent break, not a compatible
  one.** FastMCP filters `get_prompt` arguments to the declared
  signature, so a retired name means a *retired call succeeds* and
  silently drops its options.
- **`prompts_impl._split_tail` splits on whitespace only**, never a
  shell lexer, so an apostrophe in English does not raise.
- **Every prompt declares its own `value_flags` / `multi_word_flags` /
  `known_flags`**, and `tests/unit/mcp/test_prompts_flag_vocabulary.py`
  checks each advertised flag survives the parse.
- **Flag vocabularies are transcribed from the target command's Typer
  signature** and checked both ways by
  `tests/unit/mcp/test_rendered_commands_are_runnable.py`.
- **Bodies read flags via `TailParse.flag_on()`**, which answers for
  both `booleans` and `values`, and `repeats_of()` for repeatable values.
- **`_refuse_unless_clean` is the one guard and all seven bodies call
  it** — it refuses on a missing flag value or a repurposed one, because
  both make the rendered command wrong.
- **`+tag` / `-tag` atoms are read from the leading run only** —
  otherwise a question *about* option flags has its words eaten.
- **`ground` parses flags from the leading run only; `ask` parses none
  at all.** Both tails are mostly a question.

## Data shapes

- **`Span`** (`markdown_spans.py`): `(heading_path, body, code_fence,
  start_line)`. `code_fence` marks spans inside fenced blocks.
- **`PageRead.spans: list[Span]`** — populated at read time.
- **`Citation.heading_path: list[str] | None`** (default `None`).
- **`ClaimCitation.quote: str`** — validated as a substring of the cited
  span's body.
- **`ArchivistDigest`** — `no_coverage` is true only when the dispatch
  *succeeds* and returns zero hits. `transient` marks *dispatch*
  failures, so a caller can distinguish "the daemon failed" from "the
  corpus has nothing".

All forward-only; additive fields default to safe sentinels.

## Dual-source routing

Library collections are primary; wikis are secondary. On slug
collision the library hit replaces the wiki hit entirely — no merged
third value. Wiki-only hits render with a `[secondary] ` marker.

## Ingest invariants

`library/ingest.py::_process_item` compares the incoming `source_hash`
against the mirror's frontmatter. **Three states, not two:** matching
hash skips, differing hash conflicts, and *no hash* is neither. A
two-state test (`if existing_hash and existing_hash == item.source_hash`)
fell through to the conflict branch and reported an empty left-hand side
as a disagreement. It is now `mirror-unmanaged:<slug>:no-source-hash`;
the outcome is unchanged (fail loud, preserve the page).

A **self-ingest** — `--batch` pointed at a collection directory — is
the degenerate case of that third state and is refused outright
(`SelfIngestRefused`, exit 2). The slug comes from the source's relative
path and the target is `coll.dir / f"{slug}.md"`, so every page resolves
its own target onto itself. Checked by containment, not equality.

## Page-type conventions

Per-type required `## <Heading>` sections are declared in
`src/lies/schema/default_schema.md` and parsed at wiki-open time. Lint
surfaces `missing_required_section` (`safe_to_fix=False`); the writer
refuses writes omitting them. Override via `<wiki>/schema.md`. Match is
literal-substring — `## Evidence` matches, `### Evidence` does not.

## Known flakes

Measured failures that are real and not a LIES bug. Recorded so the
next agent does not re-derive them or retry a mitigation already
measured and rejected.

### `qmd embed` aborts on the CUDA VMM reservation — fixed

Intermittent hard abort: `cuMemAddressReserve(&pool_addr,
CUDA_POOL_VMM_MAX_SIZE, ...)` → `CUDA error: out of memory` →
`ggml_abort` at `ggml-cuda.cu:492`. **The label is false** — the call
reserves 32 GB of *virtual address space*, and peak embed usage is
4775 MiB at 38% with ~20 GB free on a 24 GiB card. WSL2-specific (same
WDDM stack as Windows, where `node-llama-cpp#580` reports it). No
runtime env var exists; `GGML_CUDA_NO_VMM` is compile-time, and the
runtime fallback (#610) is still open.

```bash
make qmd-backend-fix      # rebuild with VMM out, patch both copies, verify
make qmd-backend-check    # verify: VMM out AND GPU still up
```

Measured on the tag-filter suite: **4 of 5 runs abort stock, 0 of 5
patched**, with GPU verified up on the same build.

Two traps: **the rebuilt file is not the loaded file** (`localBuilds/`
holds both `bin/` and `Release/`; the rebuild writes `bin/`, the process
loads `Release/` — read the path out of the backtrace), and **a
successful `dlopen` is no evidence a library works** (two attempts
"fixed" it by leaving the GPU off, which reads as zero aborts).

A `bun install` reverts this. `tests/unit/qmd/test_cuda_backend_novmm.py`
fails when either copy regains the pool, and **skips** when no CUDA
build is present — absent is a skip, present-and-broken is a failure.

Ruled out by measurement: VRAM pressure, cross-process contention,
intra-process concurrency (`QMD_EMBED_PARALLELISM=1` measured 1 abort
with, 1 without), and retry (reverted in `cbba1b7` — retries remove the
abort and introduce `QmdTimeoutError`/`QmdWedgeError` instead).

It reproduces **only** through the tag-filter suite, which builds a
fresh per-test index. Simpler harnesses give a false all-clear, so zero
aborts in one means nothing.

### `INTEGRATION=1 pytest` is a different suite than plain `pytest`

`tests/integration/` is gated on `INTEGRATION=1`, so a plain run
reports it skipped and exits 0. That green is not evidence. CI has no
qmd daemon and skips the daemon-dependent tests, so **run it locally**.
What survives: `test_tag_filter_end_to_end.py` intermittently fails with
`QmdTimeoutError`, **a different subset each run**. The varying subset
is the diagnosis — a logic bug fails the same tests every time.

### `store_collections` can be emptied by qmd itself

`lies qmd status` reported `collections: 0` with drift on all 117
collections for an intact corpus. qmd's `syncConfigToDb`
(`dist/store.js:887`) deletes every row the external config does not
name and early-returns while `config_hash` matches — so a config
momentarily declaring zero collections empties the table and the hash
written for that empty config then matches, making it self-perpetuating.
Retrieval stays healthy (the daemon does not sync; `validate_scope`
reads the daemon's `status`), which is why it is hard to spot. It
repairs on the next store open with a mismatching hash. **Check the
`collections:` block in `~/.config/qmd/index.yml` first** — re-syncing an
empty config is what emptied it. `registry_divergence` names the event.

### A stopped qmd daemon is diagnosable only if the log survives

qmd truncates `mcp.log` on every start, destroying the artefact that
would explain an unexpected death — and LIES recycles routinely. `_down`
now copies it to `mcp.log.<stamp>` before stopping, keeping 5. If a
daemon dies again, look in `~/.cache/qmd/` for preserved generations.

## Quality gates

`make check` runs lint + typecheck + format + unit tests. `make test`
adds the integration suite. Pre-commit hooks wrap the same targets.

The per-test budget gate (`tests/unit/conftest.py`) fails a non-slow
test whose call phase exceeds 0.15s — but only after re-running it in
isolation, keeping the **minimum of up to 3 passes**. One draw from a
distribution this noisy is not a measurement (same test, same commit:
0.143 s once, 0.016 s across twelve more, limit in between). A breach
that clears is reported as noise and the run passes. Reaching for
`@pytest.mark.slow` to silence a gate failure removes the test from the
default run rather than fixing anything.

## References

- Project overview: `README.md`
- Makefile targets: `Makefile`
- Release notes: `CHANGELOG.md`