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
  **0.99x concurrency ratio** (29.48s vs 29.18s serial — the second
  waited for the first, the third for both; a ratio below 1.0 is a
  slowdown, not a speedup). So `Promise.all` over `search` calls
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
  holder PID, the elapsed wait and the budget, and **all four reach
  the caller as code, not as a prompt instruction.** The other three
  answer in milliseconds and `read_library_bodies` issues one `get`
  per path, so gating them would park a whole batch behind the
  slowest query in the process — the queue they would join is one long
  query, not each other.

  **The async gate is reentrant on one context, and that reentrancy has
  a latent hazard — do not build the shape that reaches it.**
  `qmd_lock_async` snapshots `_held_depth`, a `contextvars.ContextVar`,
  at entry, and contextvars propagate *into* `anyio.to_thread.run_sync`
  workers. A future path that acquires the qmd lock and then dispatches
  a `query` through `_run_blocking` would carry a non-zero depth into
  the worker thread, take the reentrant branch, and run the query with
  **no inode held** — silently, with no error and no log line. Not
  reachable today: the only `qmd_lock_async` holder is `daemon_tool`,
  and no `with_qmd_lock`-wrapped helper calls `daemon_tool`. Re-verify
  that before assuming it still holds.

  **Awaiting in the async gate buys thread tokens, not loop
  protection.** The `search` tool body is a sync `FunctionTool`
  dispatched through `anyio.to_thread`, so the wait already runs on its
  own loop on a worker thread and the daemon's serving loop is never
  blocked either way. A blocking poll would park one of anyio's 40
  default thread tokens for the length of the wait; awaiting parks none.

  **The last hop to the reader is deterministic; do not make it
  model-mediated again.** `register_librarian_tools`' `_search`
  closure records every envelope into a caller-supplied
  `SearchDiagnostics`, and `librarian_agent_run` passes the bundle
  through `with_search_diagnostics` before returning it. The
  librarian is an LLM, and `LibrarianOutput` is its `output_type`,
  so the model's own `transient` / `fallback_reason` are claims
  rather than facts — before this existed, the only route from the
  lock's holder PID to the reader was the model copying a string,
  and a paraphrase silently dropped the facts the operator needs.
  The prompt still tells the model to mirror the field; that is
  hygiene for the bundle the synthesizer reads, **not** the delivery
  mechanism, and both are pinned separately so neither can be
  removed on the strength of the other. The record is a keyword-only
  argument rather than a module global because the MCP server serves
  overlapping `lib_ask` calls.

  Two bounds on the overlay, both pinned: a bundle whose excerpts are
  all **attested** is returned untouched (it came from real page bodies
  after a successful retry, and forcing `transient` onto it would
  discard retrievable evidence), and a recorded failure is
  **sticky** across a later clean search (the 120 s budget was
  already spent queueing; a successful retry does not un-spend it).

  **Fabrication is closed at the path level; the text level is a
  stated limit.** An earlier version of this section called the hole
  unclosable: a model that *asserted* excerpts it did not have got
  its bundle back untouched and `_ask_impl` rendered prose citing
  pages nobody read. It was not unclosable — the recorded truth was
  already in hand. Both tool closures feed the same per-dispatch
  record (`_search` records the envelope's `hit`/`hits[*].path`,
  `_read` records the keys of its `{path: body}` result), so
  `SearchDiagnostics.attested_paths` is the set of paths a tool
  actually returned, and `with_search_diagnostics` **drops any
  excerpt whose `slug` is not in it**, naming the drop in
  `fallback_reason`. A total drop yields `transient=True` and
  `no_coverage=False`, so `_ask_impl` takes its inconclusive branch
  and the reader learns the librarian invented the bundle rather
  than that the library has nothing.

  **All three dispatch sites carry a record, by two mechanisms.**
  `librarian_agent_run` (`lib_ask`) and `ground`'s legacy F18 path
  (`grounding.py`, reached when a tag/exclude resolves to zero
  collections) thread a `SearchDiagnostics` as a keyword-only argument,
  and `ground` forwards the bundle's `transient` / `fallback_reason`
  onto `ArchivistDigest`. The orchestrator **cannot**: it registers its
  tools once, for the agent's lifetime, and `agent.tool(name=…)`
  raises on a double-register, so a record there would have to be
  captured at registration and shared across dispatches — the exact
  cross-dispatch leakage the record exists to prevent.

  So the orchestrator binds the record to the *dispatch* instead.
  `Orchestrator._dispatch_librarian` wraps `run_sync` in
  `search_diagnostics_scope`, which sets a `ContextVar`; the tool
  closures **read** that variable rather than capturing it, and a
  double-register is never attempted. Measured rather than assumed,
  because this branch has already been bitten by a ContextVar going
  somewhere unexpected (`qmd/lock.py`'s `_held_depth` propagating
  into `anyio.to_thread` workers):

  - pydantic-ai's `run_sync` wraps the run in an `asyncio.Task`, and
    a `Task` copies the current context at creation — so the closures
    see the caller's binding. Verified by driving a real
    `librarian_agent` with a `FunctionModel`, not by reading the docs.
  - a **sync** tool function is dispatched through
    `anyio.to_thread.run_sync`, which copies the context a second time.
    Same propagation, same mechanism, working in our favour.
  - two overlapping dispatches on two threads, barrier-synchronised
    so they really interleave, each keep their own `attested_paths`;
    two overlapping `asyncio` tasks on one loop likewise, and the
    binding is restored on exit.
  - a dispatch with nothing bound behaves exactly as it did before,
    and an explicitly passed record still wins over the ambient one —
    the ContextVar is a dispatch channel, not a replacement for the
    argument.

  `tests/unit/test_librarian_dispatch_record.py` holds all five.

  **What is left on that path is `_validate_claim_citations`, and it
  is narrower than it looks.** The quote is checked against the
  bundle's **own** excerpt bodies, so invented *text* on a real page
  is dropped while a wholly fabricated page passes. Say that rather
  than "the orchestrator validates citations".

  **The oracle is hits ∪ reads, not the envelope's hits.** Step 3
  says the model picks from hits, but `read` accepts any path the
  library or a wiki resolves, so a model that followed a link out of
  a body and read that page produced evidence the envelope never
  named. Keying on hits alone would drop it — discarding
  retrievable evidence to enforce a rule the tool surface never
  imposed, which is the trade being closed. `read` keys beginning
  with `_` are skipped: `_missing` is a synthetic slot, not a page.

  **What still gets through: invented *text* for a real path.**
  Attestation is path-level, so a model that reads a page and then
  attributes a passage that is not in it passes. Closing that needs
  the bodies, not the paths — the record would have to retain
  `path -> body` (and the hit snippets, for the snippet-only arm)
  and check each `Span.body` as a substring of the recorded text.
  That is a real trade rather than a small addition: a model that
  reflows a paragraph or joins two spans produces a body that is not
  a substring of the page, and every such excerpt would be dropped
  as fabricated. The false-positive rate is not measurable from
  here, and discarding true evidence is the worse of the two
  failures — so the check is written down, not shipped. Two tests
  (`test_a_span_body_is_still_not_checked` and the oracle pair in
  `tests/unit/mcp/test_excerpt_attestation.py`) hold both halves:
  the limit is asserted so it cannot be forgotten, and so a future
  text-level check has to change the test and this paragraph
  together.

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
  loop A carries an A-bound lock (`fastmcp/client/client.py:228` via
  `:587`, `anyio/_core/_synchronization.py:164-168`) — but that alone
  does not explain a sequential cross-loop call succeeding, so treat it
  as a hazard to design around rather than a proven cause of the hang.

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
  seconds past its own deadline — and that ceiling was unreachable
  until 2026-10-10.** The path under the lock is
  `_call_daemon_tool` → `_call_with_recovery` → `_recycle_or_report` →
  `recycle_qmd_daemon(ready_timeout=30.0)`, plus `_reap_qmd_daemon`'s
  up to 4 s of `time.sleep` and `_spawn_qmd_daemon`. A wedged query
  therefore blocks `lies qmd embed`/`update` host-wide for tens of
  seconds after its own 60 s read deadline. That is a deliberate trade
  — the inode is what makes the CUDA VMM reservation race unreachable
  — and **it is not a deadlock**: `qmd/daemon.py` takes no qmd lock,
  and the spawn shells `qmd mcp --http --daemon` directly, so the
  recycle never waits on the lock it is holding.

  **Worst case: a ~30 s floor and a ~61 s ceiling, both measured.**
  Measured
  2026-10-10 against the live daemon: the recycle's probe caught
  `(httpx.HTTPError, mcp.MCPError, OSError)`, but a stopped daemon
  raises a **bare** `RuntimeError` — fastmcp wraps every connect
  failure as `RuntimeError("Client failed to connect: ...")` with the
  vendored `httpx2.ConnectError` on `__cause__`
  (`fastmcp/client/client.py:1045-1048`, `_connection_failure`). Neither arm
  matched, so the loop escaped on **probe one** in ~0.3 s: it never
  retried, never spent `ready_timeout`, and never reached its own
  `QmdRecycleFailed`. The "tens of seconds" above described a path
  nobody had walked — and the practical damage was a *false* report,
  since `_recycle_or_report` swallowed the escape and told the caller
  the restart had failed while the daemon it had just started was
  coming up. The tuple now covers `RuntimeError`.

  Measured by scaling every constant by 1/100 and driving real
  exhaustion through the genuine reap, spawn, `_run_qmd` reader loop,
  probe loop and deadline
  (`tests/integration/test_qmd_recycle_ceiling.py`):

  | scenario | measured (real scale) |
  | :--- | :--- |
  | prompt status, prompt spawn | **30 s** (floor) |
  | status "running", SIGTERM missed | **34 s** |
  | prompt status, **spawn hangs** | **45 s** |
  | **`qmd status` hangs**, prompt spawn | **45-59 s**, a **cliff** |
  | both hang | **61 s** (ceiling) |

  **The `qmd status` row is a range and must be copied as one: the row is
  a cliff, not noise.** The 30 s budget admits a second in-loop `qmd status`
  call only while `2 x bound + poll_interval < budget`, i.e. bound <
  0.1475 at 1/100 scale; `STATUS_TIMEOUT_S / N` is 0.1500, so the shipped
  figure clears the boundary by 0.0025 scaled — **0.25 s of headroom at
  real scale** — and one bound further back costs a whole
  `STATUS_TIMEOUT_S`.

  **Scaled:** `STATUS_TIMEOUT_S`, `ready_timeout`, `poll_interval`, the
  reap's `grace`/`poll`, **and** `_subprocess._POLL_INTERVAL_S`. That
  last one is neither a timeout nor a sleep — it is the reader loop's
  `sel.select()` granularity — so it does not scale by itself, and an
  unscaled 0.1 s survives the rescaling. With the reader loop real the
  ceiling reads 61 s scaled and 86-88 s unscaled, so leaving it out would
  put the reported figure 40% low. Measured by sweeping: 0.1450 → two in-loop calls →
  **59.4 s**; 0.1470 → one → **44.9 s**, so the row's honest range is
  **44.9 to 59.4 s** — the 14.5 s swing is 0.25 s of real-scale
  headroom, and the harness happened to sit on the near side. A reader
  who copies 46 s is wrong by up to 14 s in either direction.

  Three more traps in that table. **The spawn has its own 15 s bound**, so
  "prompt status" does not mean prompt everything, and the spawn-hang
  case dominates the middle. **The grace windows are reachable in
  exactly one row** — a hanging `qmd status` reports
  `running=False`, so the reap returns at its first line and spends
  neither. That row's *measured* cost is a property of the runner, not
  of the code: the harness's window is 20 ms wide and polls every 0.5 ms,
  so a host that cannot return from a 0.5 ms sleep in under 20 ms
  measures each window as one sleep long (4.8 ms on an oversubscribed
  host; 27.7 s at real scale on the CI runner against ~4 s here). The
  harness bounds it from above with the timer it observed rather than
  with a fixed ceiling, and pins the structural claim — one row spends
  both windows — exactly.
  A **hanging `qmd status` makes the
  ceiling lower, not higher**: `qmd_daemon_state()` then reports
  `running=False`, so `_reap_qmd_daemon` returns without killing
  anything and spends neither grace window — which is why the "~80 s"
  this bullet previously quoted was not merely loose but *unreachable*.
  And the **45-59 s row is the spawn-hang row to within 1 s at its near
  end, by coincidence rather than equivalence**: in one the extra 15 s is
  the spawn's, in the other it is the reap's status call, with the same
  30 s of budget behind it — so a reader who treats them as one
  measurement twice will discount the spawn.

  An earlier version of this bullet called that row "unstable" on the
  strength of a 2 s spread. Counting `qmd status` calls across 20 runs of
  the harness that produced it gave **3 in 20 runs out of 20** — the call
  count never moved, and the spread was `time.sleep` jitter inside a
  stubbed `_run_qmd`. The cliff is real; the earlier observation was not
  of it.

  **The reader loop must be real for any of this to mean anything.**
  `_subprocess._POLL_INTERVAL_S` is read only inside `_run_qmd`'s
  `sel.select()`, so a harness that replaces `_run_qmd` never reads it
  and its rescaling is inert — measured: stubbed, patched and unpatched
  give identical distributions; with the real loop, 61 s scaled against
  86-88 s unscaled. *Transcribed, not re-measured at commit time.*
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
  raises `QmdDaemonWedged` carrying a `mcp.log` tail; no transparent
  retry, because a fresh daemon re-wedges on the same payload.
  *Unreachable* recycles and retries once. A *protocol-level rejection*
  is not a transport failure and re-raises unchanged. `classify_call_error`
  is the only place that decides.
- **`last_output` must come from the daemon that actually wedged.** The
  two call sites read on opposite sides of their recycle and both are
  correct. A tail attributed to the wrong daemon is worse than none.
- **A retry's failure is classified, not assumed.** After a recycle,
  take one of three routes: an unowned reason propagates, a re-wedge
  raises `QmdDaemonWedged`, only still-unreachable raises
  `QmdDaemonUnavailable`. Collapsing them tells the operator to start a
  daemon that is already running. (Both names were written here without
  their `Daemon` infix, which matches nothing in `qmd/access.py`.)
- **Deliberate taxonomy gap.** `HTTPStatusError` (5xx) and
  `LocalProtocolError` (client-side malformed) classify as
  `passthrough` — no recycle. `RemoteProtocolError` keeps the
  recycle-retry path. All three are pinned in `tests/unit/qmd/test_access.py`.
- **The taxonomy matches exception class *names*, not httpx types.**
  fastmcp vendors its own httpx as `httpx2`; a dead session arrives as a
  bare `RuntimeError` with the real error on `__cause__`
  (`fastmcp/client/client.py:248` via `:1045-1048`). A taxonomy
  written against the httpx LIES declares matches none of them. Re-derive
  by probe before changing the matcher.

  **Confirmed by probe, 2026-10-10** — this was recorded as a hypothesis
  and is now a measurement. `fastmcp/client/client.py:1045-1048` calls
  `_connection_failure`, whose own docstring says it presents a dead
  session identically "wherever it is noticed" and wraps anything that is
  not `httpx2.HTTPStatusError | MCPError` into a bare `RuntimeError`. The
  full chain for a closed port is `RuntimeError` → `httpx2.ConnectError`
  → `ExceptionGroup` → `CancelledError` → `anyio.WouldBlock`.
  `httpx2` is a *separate installed distribution* (2.13.1) from the
  `httpx` LIES imports (0.28.1), so `httpx2.ConnectError` is neither an
  `httpx.HTTPError` nor an `OSError`. HTTP-level not-ready (a booting
  daemon answering 404/503) is different and *was* already caught: it
  arrives as `mcp.shared.exceptions.MCPError`. **Code against the
  wrapper, not the vendored module name** — importing `httpx2` would make
  a fastmcp re-vendor a hard LIES import error.
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
- **`qmd mcp --http --daemon` has `--port` and has no `--data-dir`.**
  Read from qmd 2.5.3's `dist/cli/qmd.js`, `case "mcp":`: the only
  inputs are `cli.args[0]` (`stop`/`status`), `values.http`,
  `values.port`, `values.daemon` and `values.index`. `--port` is
  honoured (`const port = Number(cli.values.port) || 8181`) and
  `qmd mcp --help` does **not** list it — that prints the *global* help,
  which is why the flag's existence is a source-read fact and not a
  `--help` one. There is no data-dir flag anywhere in the daemon path;
  `XDG_CACHE_HOME` is the lever, and it relocates `index.sqlite`,
  `mcp.pid` **and** `mcp.log` together, appending its own `qmd/`
  segment (`X` → `X/qmd/`).

  **`mcp.pid` is keyed to the cache root and NOT to the port.** A second
  daemon sharing the real cache root on a *different* port overwrites the
  machine daemon's pidfile, and both `qmd_daemon_state` (via
  `qmd status`) and `_reap_qmd_daemon` read exactly that file — so the
  reap would SIGTERM the wrong process. `qmd.js:4008-4013` also refuses
  to start while a live pidfile exists, so the hazard runs both ways: a
  stray blocks the operator's next real start.

  **The HTTP daemon binds a NAME, not an address — the literal
  `localhost`, and `--port` does not change it.** qmd 2.5.3's
  `dist/mcp/server.js:671` calls
  `httpServer.listen(port, "localhost", () => resolve())`; there is no
  `options.host` chain and `QMD_HOST` appears nowhere in `dist/`
  (`grep -rn QMD_HOST dist/` is empty), so nothing selects the family.
  Node resolves the name to whichever entry `/etc/hosts` lists
  **first** and binds only that. On a runner listing `::1 localhost`
  ahead of `127.0.0.1` that is `[::1]`, and `connect(("127.0.0.1",
  port))` is refused while `connect(("localhost", port))` succeeds —
  measured on the CI runner, where qmd logged `listening on
  http://localhost:57703/mcp` on the requested port while the IPv4
  dial got `ECONNREFUSED`, and reproduced locally by bind-mounting an
  IPv6-first hosts file. **`--port` selects the port only**; nothing
  in it selects the family. So any code or test that probes a daemon
  it did not spawn must probe the *name* qmd bound — **and must not
  pin the probe's family**, since an `AF_INET` socket filters name
  resolution to v4 and reproduces the exact refusal the name fixes
  (`socket.create_connection`, not `socket.socket(AF_INET, …)`).
  LIES addresses the daemon by the name in both places that name it
  (`config.DEFAULT_QMD_URL`, `lifecycle._HOST`), pinned by
  `tests/unit/qmd/test_daemon_host.py`. The diagnostic that settles
  this class of failure is qmd's own `mcp.log`: `server.js:696` logs
  `QMD MCP server listening on http://localhost:${actualPort}/mcp`,
  so a "nothing is listening" report that never reads it has not
  distinguished *did not start* from *started somewhere else*.

  **That hazard is a caller precondition, not a spawn-argument guard.**
  `_spawn_qmd_daemon(port=...)` without `data_dir=` is *allowed*, and
  every production caller reaps first, so no live pidfile is held when
  the spawn runs. An earlier version refused the pairing, which broke
  the only legitimate use — restart *the* daemon on *its* configured
  port, sharing its cache root — and left `LIES_QMD_URL=...:9000`
  spawning a stray on 8181 while the probe polled 9000. Every spawn now
  threads the port from the URL it will probe
  (`daemon.daemon_port_from_url`). That is **six call sites across four
  reap-then-respawn locations**: `capability.py`,
  `access.daemon_tool` (`access.py`), `recycle_qmd_daemon`, and three
  paths in `daemon.py`'s `ensure_qmd_daemon`. `access.daemon_tool`'s
  was missed by the first fix and is the hottest of the four, not the
  coldest: `_is_daemon_stale()` is true on essentially every call after
  any write. `tests/unit/test_agents_line_refs.py` pins the cited file
  set by content and asserts it is *exhaustive* against the files that
  actually spawn, so a fifth site inherits the pin instead of escaping
  it. The citation is file-level on purpose: a `file.py:N` pin into a
  file this branch edits goes red when a comment is added above the
  cited line, which is no behavioural change.

  **The sidecar records a "scope", and a scope is not an index.** Four
  call sites used to pass four different values (`wiki.wiki_dir`,
  `wiki.data_root`, sidecar-or-`library_git_root()`, `library_git_root()`),
  and two differed by construction — `wiki.wiki_dir` *is*
  `data_root / "wiki"`. The system coped only because the value never
  reached the spawn. All four now pass `daemon._canonical_scope()`, so
  `check_scope_match` compares like with like; `_spawn_qmd_daemon`'s
  `data_dir=` — the one argument that really does move the index — has
  no production caller. `_recycle_scope()` no longer reads the sidecar
  back: that made the record self-confirming and perpetuated stale paths
  after a wiki moved.
- **Every qmd subprocess runs with `NO_COLOR=1`**, forced rather than
  inherited, so an operator's shell cannot change what LIES parses.
- **The `⠋ Gathering information` spinner is `ipull`, not qmd**, and
  only during a model download. It is not gated by `NO_COLOR`; the lever
  is a warm model cache.
- **A release removes the triad it acquired, not the one the
  environment names now.** Both poll loops return the winning
  `_Attempt`, which carries the triad it ran against, and both release
  paths hand that to `_release`. `_register_holder` takes it too. Each
  side used to call `_lock_paths()` for itself, so a
  `LIES_QMD_LOCK_PATH` that moved between acquire and release split one
  acquisition across two sets of files: the release unlinked a lock
  nobody held and left the create-lock, the `.pid` sidecar and the
  `.state.json` heartbeat behind with no fd pointing at them. The leak
  is silent, and the leftover heartbeat is worse than the leftover
  lock — it is what the *next* acquire reads to decide whether the lock
  in front of it is stale. Both halves are unit-tested in
  `tests/unit/qmd/test_lock_release.py`. A test session that acquires
  and then dies without unwinding is not covered by any in-process
  hook; `tests/conftest.py`'s session-scoped teardown bounds it, and
  `SIGKILL` is the one exit nothing bounds.
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

## `ground()` is public API — its diagnostics are NOT dead code

**`src/lies/mcp/__init__.py` declares `__all__ = ["ground", "mcp"]`
and re-exports `ground` "for in-process callers ... so callers have
one import surface". That is a supported surface, not an
implementation detail.** Anyone reasoning about reachability from the
in-tree call graph will get this wrong, and has: a whole-branch review
flagged `ArchivistDigest.fallback_reason` and the two `except
QmdLockBusy` arms in `mcp/grounding.py` as possibly unreachable,
because `ground()`'s only in-tree caller is `synthesize()`, and
`synthesize()` is called only by tests.

That is true of the **call graph** and false of the **contract**. An
in-process caller — the stated purpose of the re-export — reaches
`ground()` and every diagnostic on it. Deleting those arms because
`grep` finds no production caller would break the public surface and
would be stopped by this section, not by a code comment.

**Why the diagnostics look rare even though they are reachable:** the
MCP tool path routes through the librarian instead. `lib_ask` goes
`librarian_agent_run` → `register_librarian_tools`' `_search` →
`search.fn`, and *that* path carries the lock's facts via
`SearchDiagnostics` + `with_search_diagnostics` (see the qmd access
seam section above). `ground()` reaches `access.daemon_tool` directly
on its unscoped and tagged fast-paths, so the exception arrives as a
real exception and its own `except QmdLockBusy` arms catch it —
`grounding.py:403` and `:472`. Two mechanisms, two different
shapes, both live.

So: the arms are reachable, the digest field has a reader
(`synthesize`), and removing either is a contract break. Do not
propose it as dead code.

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
  otherwise a question *about* option flags has its words eaten. A token
  is a filter only when what follows the sigil looks like a tag: a `:`
  qualifier (`c:opencode`) or two or more characters, so `-1`, `--` and
  `-e` stay question text.
- **`ground` parses flags from the leading run only; `ask` parses none
  at all.** Both tails are mostly a question.
- **Question text is returned verbatim, newlines and all.** Only the
  filter run is tokenized; the rest is sliced out of the original string
  by offset. Rebuilding it by joining tokens collapsed every internal
  whitespace run, so a pasted code block reached the search run-on.
- **`_split_tail`'s fields are not interchangeable.** `values` is
  last-wins, so a body rendering a *repeatable* flag (`--tag a --tag b`)
  reads `repeats_of()`; `ignored_values` carries a boolean written
  `--all=true` (the flag is on, the value means nothing) so it is
  reported rather than dropped; `repurposed` carries an undeclared value
  flag whose following token fell through to `positionals`, which is how
  `show --tag cli` otherwise turned the flag's value into a collection.
- **Per-verb flag vocabulary, not one union set.** Too narrow and a real
  flag is reported as a typo with its value falling into a positional;
  too wide and a flag the verb lacks is parsed as a boolean and never
  rendered. Both errors are silent, and only an exact transcription of
  the Typer signature avoids either.
- **The slash form binds one token and drops the rest.** That is why the
  server also registers the seven prompts as *tools*: tool-call
  arguments are not pre-tokenized, so a multi-word question reaches a
  prompt whole there.

## Data shapes

Per-field contracts live here rather than in the class docstrings, which
the prose budget keeps short. Each entry lists only what a caller cannot
infer from the field name or its type.

- **`Span`** (`markdown_spans.py`): `(heading_path, body, code_fence,
  start_line)`. `heading_path` is the nested ATX text from H1 down to the
  span's parent, empty above any heading. `body` is verbatim, and for a
  code-fence span it is the *inner* content with delimiters stripped.
  `code_fence` marks spans inside ``` or ~~~ fences; consumers exclude
  those from prose excerpts.
- **`PageRead.spans: list[Span]`** — populated at read time.
- **`Citation.heading_path: list[str] | None`** (default `None`).
- **`CitationSlug` shape**: `slug` is always `<collection>/<page>`, never
  a bare page segment, so an agent can tell `setup.md` in two collections
  apart.
- **`ClaimCitation.quote: str`** — validated as a substring of the cited
  span's body.
- **`ArchivistDigest`** — `no_coverage` is true only when the dispatch
  *succeeds* and returns zero hits. `transient` marks *dispatch*
  failures, so a caller can distinguish "the daemon failed" from "the
  corpus has nothing".
- **`Citation.source` on `lib_ask` is model-asserted, not verified.**
  `synth._ask_impl` applies no citation validation: the orchestrator's
  `_validate_claim_citations` and `_thread_heading_paths`
  (`orchestrator.py:2082`/`2085`) run only on the orchestrator path. So
  `lib_ask` emits whatever citations the synthesizer asserted. A path
  the librarian never handed over still becomes a `Citation`, defaulted
  to `source="wiki"` — see `query/citation.py` for why that default is
  the orchestrator's own mapping and not a verdict on the page. The
  consequence: a hallucinated path renders as a citation rather than
  being dropped, so `lib_ask` emits unverified citations where it once
  emitted none at all. `heading_path=None` is contract-correct under the
  rule above. A caller that needs the citation to be real must resolve
  it through `read`.

- **`AcquireResult.status`** (`utils/exclusive.py`) is one of four
  outcomes, and `fd` is meaningful in each: `"acquired"` and
  `"dead_reaped"` both carry a valid fd (the second means a stale holder -
  dead PID or expired heartbeat - was reaped and the retry won);
  `"busy"` and `"indeterminate"` carry `fd=-1` plus `holder_pid` and
  `holder_started_at`, which exist so raise sites can name the holder.
  The last two differ in *liveness being undecidable*: `"busy"` means a
  fresh heartbeat said alive, `"indeterminate"` means `pid_alive_fn` said
  indeterminate **and** the heartbeat expired, and the primitive
  deliberately did not reap. Callers that omit the `pid_path` +
  `state_json_path` envelope get `None` for the busy path (legacy
  semantics) and can never reach `"indeterminate"`.
All forward-only; additive fields default to safe sentinels.

### The write path

`WikiMemoryService.apply_plan` is the sole owner of wiki mutation, and
three rules there are load-bearing:

- **Strip `wiki/` twice.** Page-writer emits `wiki/`-prefixed paths and
  every resolver joins onto `wiki.wiki_dir`, so one strip of a
  doubled `wiki/wiki/log.md` leaves the candidate at
  `<root>/wiki/wiki/log.md` and the system-file guard misses. The apply
  and validate paths must strip identically or they disagree on the same
  plan.
- **`wiki/index.md` and `wiki/log.md` are not writable through an op.**
  `index.md` is a render derivative; `log.md` belongs to
  `append_log_entry`. The only carve-out is `PageUpdate` on `index_path`,
  which is the repair agent's `UpdateIndex` pathway — a receipt surface,
  not a permanent write, with the per-op catalog upsert keeping
  `catalog.db` in lockstep.
- **A no-op commit returns an empty receipt**, never changed pages that
  were not applied at the git level.

`library/writer.py` mirrors that envelope, and four of its steps are
load-bearing:

- **A fresh library directory is bootstrapped to a git repo** on the first
  writer creation. Without it, `lies ingest` writes mirror files to disk
  and then raises `LibraryAtomicCommitFailed` at the `git add`.
- **Deletions need an explicit `git add -u`,** because `atomic_commit`
  stages the given paths only — a tracked file deleted from disk is never
  staged, and every re-ingest accumulates ghost entries in HEAD. It is
  `-u`, not `-A`: `-A` would also stage files the writer does not claim.
- **The last-write marker is touched at the machine-global cache path**
  before the qmd hook, so the daemon's mtime comparison sees this commit's
  mtime rather than the hook's.
- **A `QmdLockBusy` from the embed hook is re-raised, not swallowed.** The
  message must name the holder PID and the contended inode, because that
  is the one operator step distinguishing a contended host from a broken
  one. `waited_s`/`max_s`/`holder_pid` are all guarded against `None`,
  which the signature accepts on translation paths like
  `WikiFlockIndeterminate`.

### Provenance

`derived_from` is comma-joined in storage and `list_provenance_pages`
splits on `,` with no escaping, so a slug containing a comma is
ambiguous by construction. `raw/` is immutable: `apply_plan` rejects a
read or a write under it.

## Dual-source routing

Library collections are primary; wikis are secondary. On slug
collision the library hit replaces the wiki hit entirely — no merged
third value. Wiki-only hits render with a `[secondary] ` marker.

Retrieval (`query/synthesizer.py`) runs **two** `qmd_query` passes: a
primary pass scoped to the resolved library collection set, and a
secondary pass scoped to the wiki-rooted collection. Only the primary
pass carries the caller's `collection_filter`; local wiki content is
supplementary and is never gated by the caller's tag filter. Pages are
deduped on `(rel_path, source)` — without that, the body says "based on
2 wiki pages" while the dict the synthesizer reads holds one.

**A collection name is always an addressable tag**, whatever its
`tags` field says, and the rule lives at the retriever boundary
(`_collections_matching`), not in `tag_expr.resolve` — the resolver only
validates that every atom exists in the available tag set.

**The CLI-path `qmd query` pays a full model load on every call**, which
the daemon does not: it keeps models warm for
`DEFAULT_INACTIVITY_TIMEOUT_MS`. Measured with a warm model cache, 3.9–10.9s
typical with a 31.2s and a 67.7s tail, which is why the idle bound on
that path is sized to the model-load tail rather than left at 30s.

## Lazy imports are a contract

`import lies.cli` must not load `fastmcp`, `pydantic_ai`, or the
orchestrator (`tests/unit/cli/test_cli_lazy_imports`). Two consequences
bite repeatedly:

- **`LOAD_GLOBAL` does not consult a module's `__getattr__`.** PEP 562
  fires for `module.name` attribute access only, so a bare name for a
  lazily-exported helper inside a function body raises `NameError` in a
  fresh process. Resolve explicitly — `globals().get(name)` then
  `__getattr__(name)`. See `cli/page.py`, `cli/qmd.py`, `wiki/layout.py`
  and `library/writer.py`.
- **Heavy names are reachable as attributes for the tests' benefit.**
  Several tests do `monkeypatch.setattr("lies.cli.Orchestrator", ...)`,
  so the package `__getattr__` must resolve them; it loads each on first
  access, which is what keeps the startup contract intact.

## Agent output shape

The agents use `PromptedOutput`, not the default tool output:
MiniMax-M3 ignores `tool_choice` and `response_format=json_schema` on
both endpoints, so the schema goes in the instructions and the model's
JSON text is parsed. `TestModel` cannot drive `PromptedOutput`, so those
paths are covered by the integration suite, not by a unit test. Stub the
agent's `run_sync` **at the per-instance boundary** rather than letting
`TestModel` iterate its registered tools: `TestModel` invents random
`page_ids`, so a `wiki_read` call raises `WikiPageNotFound` before the
code under test ever runs.

## The cross-process lock

`qmd/lock.py` holds a site-wide flock at
`${XDG_STATE_HOME:-~/.local/state}/lies/qmd.lock`, resolved **per
acquisition** so `LIES_QMD_LOCK_PATH` retunes without a restart. It is
reentrant on one context: a nested acquire in the same task does not
re-acquire. Without that, two composed helpers (`qmd_collection_add_or_update`
calls show, then add) made the inner call poll for a lock its own outer
call held. Re-entry is tracked by a depth counter, **not** by pid
equality — a second thread of the same process stores the same pid, so
pid-based self-recovery would reap a live lock and let both threads into
the critical section, which is the CUDA reservation race the lock exists
to prevent.

The wiki memory flock envelope lives under `$XDG_RUNTIME_DIR/lies/<wiki>/`,
not the wiki's data root, so no gitignore coordination is needed.
Releases hand back the exact path set they acquired; a release that
re-derived the paths could unlink a lock nobody holds and strand the
`.pid` sidecar and `.state.json` heartbeat the next acquire reads.

**The lock to race is `acquire_create_lock`, not `WikiMemoryService._lock`.**
The service exposes `_lock` as an in-process `threading.Lock`, which no
other process can see. Synthesis file-back is guarded by the
`acquire_create_lock` envelope, so a busy-path test must hold *that*
across a subprocess boundary — an in-process lock held in the test
process would make the very contention it is asserting on impossible.

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

Two more that are not obvious from the call site:

- **`source_hash` is the SHA256 of the raw bytes at fetch time**, hashed
  once up front. It is *not* per-document, and `ingested_at` derives
  from it, so a per-doc slice would break re-ingest idempotency.
- **The batch walk admits text documents plus anything the builder
  REGISTRY can render** (routing the latter through the same
  `_normalize_body` path `--source` takes). `should_skip_filename` is a
  *denylist* and cannot express "is a document": a `config.yaml` in a
  collection directory became a `config.md` page, 36 times over.
  A quarantined doc writes both a body and a `.reason` sidecar.

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
`QmdCommandError: qmd query failed (exit 1): … need embeddings`,
**a different subset each run**. The varying subset is the diagnosis — a
logic bug fails the same tests every time. (An earlier version of this
entry named `QmdTimeoutError`; that is not what this host reports. Both
are subclasses of `QmdError`, and `QmdTimeoutError` subclasses
`QmdCommandError`, so a reader grepping for the family still lands here.)

**The suite's skips depend on the environment, not only on the
corpus.** Two gates that once read `qmd_daemon_reachable` mean two
different things, and only one of them is about a daemon:

- *Is a daemon answering on the port?* `health.qmd_daemon_reachable`
  opens a TCP socket. True for any process on the host.
- *May qmd run its LLM?* qmd reads `process.env.CI` at import
  (`dist/llm.js:384`, `_ciMode = !!process.env.CI`) and every LLM entry
  point throws behind it — `expandQuery` at `:1058`, on the way to the
  hybrid ranking `qmd query` performs. LIES spawns qmd with the
  operator's environment unchanged (`_subprocess._child_env` copies
  `os.environ`), so `CI=true` reaches the child.

Gating a real *query* on the first condition let a stray daemon decide
the run: nothing on 8181 meant a skip, a stray there meant the query ran
and every test failed on qmd's CI refusal. A stray that answers TCP but
serves an empty index does the same to a corpus gate. So
`tests/integration/mcp/conftest.py` asks whether the daemon serves a
**corpus** (`access.qmd_collection_names()`, `validate_scope`'s own
seam), and the tag-filter fixture asks
`tests.integration.conftest.qmd_llm_operations_disabled()`. Neither
condition is implied by the other, and neither is decided by whatever
else is bound to 8181.

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