# Changelog

All notable changes to LIES are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/) adapted for
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.48.1] - 2026-10-04

Three regressions from `uv lock --upgrade`, which moved
`pydantic-ai-harness` 0.31.0 -> 0.54.0, `pydantic-ai` 2.43.0 -> 2.54.0 and
`fastmcp` 4.0.3 -> 4.0.10. The lock upgrade is included: the suite was green
against 4.0.3 only because the tool install resolved separately and took the
newer set, so the tests were not exercising the versions in production. The
upgrade is what made all three visible.

### Fixed

- **The orchestrator's agent carries a workspace again.**
  `pydantic-ai-harness` 0.54.0 changed `FileSystem` from self-rooted to
  workspace-backed: `root_dir` is now a guardrail bounding the model's file
  tools, while every operation resolves through the *run's* workspace.
  `FileSystem.before_run` calls `require_workspace`, and a run with no
  workspace attached fails at its start -- before the model is called. The
  shim set only `root_dir`, so it was half a configuration and
  `Orchestrator.run` failed on every invocation:

      UserError: `FileSystem` needs a workspace, but none is attached to this
      run. Add `LocalWorkspace('.')` ... or pass `workspace=` to the run.

  The harness documents the wiring as
  `Agent(..., capabilities=[LocalWorkspace(path)])`, and `local_workspace()`
  adds it there rather than to each run: `Agent.__init__` accepts no
  workspace, and `run_sync` rejects a `LocalWorkspace` outright ("is a
  capability ... or pass a backend such as `LocalWorkspaceBackend(path)` as
  `workspace=`"). Putting it on the capabilities list covers every run,
  including one added later, rather than the two `run_sync` call sites.

- **`collection_author_agent` declares the output type it actually has.**
  The function returned `Agent[..., AuthorOutput]` while constructing
  `Agent(resolved, output_type=cast(Any, AuthorOutput))`. The cast erased the
  union, so the checker inferred the constructor's default `output_type` of
  `str` and `ty` reported the mismatch. The comment above the `cast` claimed
  the Agent overloads do not accept `type[X | Y]`; as of pydantic-ai 2.54 they
  do. Removing the cast makes the declared type one the body supports, and
  the return annotation now spells the union out rather than naming the alias
  -- the alias does not survive as the agent's own type parameter, so a
  checker comparing the two does not see them as equal.

- **`render_mirror` is idempotent.** The writer prepended frontmatter
  unconditionally and nothing stripped an existing block, so re-ingesting a
  file that was already a mirror stacked a second block on every run -- and a
  third on the page ingested three times. `strip_frontmatter` now removes
  *every* consecutive leading block, stepping over the blank line the writer
  itself emits between them, and leaves an unterminated `---` alone so a
  markdown rule at the top of a page is not mistaken for frontmatter and
  discarded. Line endings are preserved rather than normalised: the body is
  written back out verbatim, and a CRLF document should not be rewritten to
  LF. Pinned by `test_strip_frontmatter` (six cases -- stacked blocks, an
  unterminated block, a leading thematic break, CRLF) and by
  `test_render_mirror_is_idempotent`, which renders a mirror of a mirror and
  asserts the result is byte-identical.


## [0.48.0] - 2026-10-04

### Fixed

- **`search` resolves `t:` filters and treats an unqualified atom as an
  implicit `t:`.** `_resolve_tag_collections` validated its include tree
  against `library_collection_names()` and then dropped every `t:` atom,
  so a tag-qualified query resolved to an empty scope and came back as
  `unknown_tags: ["t:activitypub"]` with zero hits. The behaviour before
  that one widened to every registered collection with
  `unknown_tags == []` — a query the caller scoped by tag, answered from
  the whole library, with nothing in the response to say so. A bare atom
  resolved as a *collection*, which is what made a `c:` prefix read as
  necessary.

  F15 already defines the opposite default: `server._collect_available_tags_mcp`
  documents that "F15 treats a bare atom as the implicit-t alias for
  `+t:tag`", and `synthesizer._collections_matching` implements the
  `t:`/bare and `c:` dispatch plus the "collection name as implicit
  self-tag" rule. `search` was the one surface that diverged from it —
  and the `ground` prompt body already told agents to pass `t:` filters
  to this tool, so the documented route produced `unknown_tags`.

  The fix delegates to that same resolver rather than adding a second
  implementation, so `search` and `ground`/`lib_ask` answer one filter
  the same way and the two cannot drift. `|` unions and `&` intersects
  over the collection sets the atoms name. Pinned by
  `test_search_tag_only_filter_resolves_the_tagged_scope` (the scope
  reaches the daemon, does not widen), `test_search_unqualified_atom_is_an_implicit_t_qualifier`,
  `test_search_c_qualifier_still_addresses_a_collection`,
  `test_search_bare_collection_name_is_its_own_implicit_tag`,
  `test_search_tag_expr_set_algebra`, and
  `test_search_unknown_qualified_tag_marks_unknown_tags`.

  `test_search_tag_only_filter_is_not_silently_widened` is replaced rather
  than kept: it asserted the drop-`t:` behaviour this fixes. Its
  invariant survives in `test_search_unknown_qualified_tag_marks_unknown_tags`.

- **Two test fixtures stubbed the registry incompletely.** Both
  `_patch_registry` in `tests/unit/mcp/test_search.py` and the fixture in
  `tests/unit/mcp/test_search_timeout.py` patched
  `library_collection_names` and left `library_collection_metas` and
  `library_collection_tags` reading the real registry. The filter
  vocabulary is built from all three, so a filter that should have
  resolved matched nothing and the tool reported it as an unknown tag —
  which, in the timeout tests, masked the daemon-boundary assertion each
  one exists to make.


## [0.47.2] - 2026-10-04

### Fixed

- **`derive_nested_slug` normalizes every path segment, not the whole
  parents string.** `*parents, tail = raw.rsplit("/", 1)` yields at most
  two values, so for a source two or more directory levels deep
  `parents` is a *single string* carrying the remaining separators
  rather than a list of segments. The normalization comprehension
  treated it as one segment, so its `.strip("-")` only cleaned the
  string's outer edges and an *interior* directory kept the leading
  dash its own normalization produced: `.claude` → `-claude`. Every
  segment has to match `[a-z0-9][a-z0-9_-]*`, so the whole slug failed
  `validate_slug`.

  The fix splits `parents` on `/` before normalizing, which is what
  the docstring and the code comment above the comprehension both
  already claimed it did — and what `scrapers/web.py` does for the
  same job (`parts = [seg for seg in p.path.split("/") if seg]`).

  Every pre-existing case had one directory level, where `parents`
  happens to hold a single genuine segment, so the defect was
  invisible to the suite. It surfaces on a source tree with a dotted or
  underscored interior directory. Observed on the live corpus: the
  `switchyard` collection's
  `experimental/craft-taskgen/.claude/skills/harbor-f2p-p2p-deep-dive/SKILL.md`
  derived `experimental/craft-taskgen/-claude/skills/harbor-f2p-p2p-deep-dive/skill`
  and raised `ValueError: invalid slug`. `_process_item` quarantines an
  invalid slug on the URL branch but not on the path branch, so the
  exception escaped and aborted the whole collection sync —
  `lies reindex --reconcile` died before indexing any collection, and
  with it the tag index that `t:<tag>` search filters read.

  Covered by `test_derive_nested_slug_normalizes_every_segment`,
  parametrized on the observed path plus a leading-dot first segment,
  an interior underscore-and-dot pair, and a dunder in the middle.

## [0.47.1] - 2026-10-04

### Fixed

- **The tag-filter qmd fixture's cleanup now covers its seeding.**
  `_seed_qmd` registers a collection with qmd and *then* embeds it.
  The embed raises on the CUDA VMM reservation flake — node-llama-cpp's
  `cuMemAddressReserve` aborting with `CUDA error: out of memory` and
  `ggml_abort` — so seeding never reached the fixture's `yield`, and a
  `try/finally` written around the yield alone never ran its `finally`.
  Observed 2026-10-03 22:46, thirteen minutes after a run with three
  such aborts: `wiki_tag-filter-lib` registered in the live index and
  left there. This is where the residue recorded under *Known flakes*
  came from; two investigations had searched session logs for an
  unexplained write.

  A cleanup that fails also no longer replaces the exception in
  flight. `_unseed_qmd` calls `pytest.fail` when a collection is still
  registered, and from inside a `finally` that hid "the embed aborted
  on the CUDA reservation" behind a hygiene message about a throwaway
  index. It rides along as a note instead.

- **A recycle that fails now says the daemon is down.** `_recycle`
  caught `QmdRecycleFailed`, logged a warning, and returned — so a
  restart that exhausted its budget left a machine-global daemon
  stopped, and the caller's error said "recycled" about a restart
  that did not happen. The wedge message named no command, and every
  qmd call on the host then failed with a `QmdDaemonWedged` nobody
  could act on. The message now appends the operator instruction when
  the restart failed, and keeps the wedge as the diagnosis.

- **The live-index residue is gone.** Four orphan `content_vectors`
  rows and five `documents` rows for `wiki_tag-filter-lib` — the
  tag-filter fixture's own page set, for a collection that was never
  registered — were removed on 2026-10-04. The index now reads **15
  collections, 5987 documents = 5987 content = 5987 FTS, 48984
  vectors**, with `integrity_check` ok, `foreign_key_check` clean,
  and all three residue classes zero through `lies qmd status`. The
  corpus is back to the 5987 these docs carried before the leak.

  Removing the documents left five `content` rows carrying vectors
  and no document, and removing those cascaded four vectors away.
  Both steps are needed to land on 1:1:1; a cleanup that stops at
  the documents trades one residue class for another, and
  `index_orphans` — which checks vectors against content — would not
  have seen the documentless content at all. A verified backup
  precedes the write at `~/qmd-index-backup-20261004.sqlite`.

### Added

- **The daemon's log is preserved across a stop.** qmd truncates
  `mcp.log` on every start, so the artefact that could explain an
  unexpected death was destroyed by the recovery attempt — and LIES
  recycles the daemon routinely. `_down` copies it to
  `mcp.log.<stamp>` before stopping, keeping 5 generations. The copy
  is best-effort throughout: a log that cannot be read or written
  never blocks the stop.


## [0.47.0] - 2026-10-03
### Added

- **`transient` on `LibrarianOutput`, and both consumers read it.** A
  librarian LLM dispatch that raised — a `UsageLimitExceeded`, a model
  outage, a response pydantic-ai could not validate after its retry
  budget — returned `no_coverage=True`, so `lib_ask` answered "No
  relevant content found in library." on the primary human-facing tool,
  naming a corpus problem for a process failure. That is the exact
  string the librarian contract designates as the canonical false
  claim. `LibrarianOutput.transient` is the new flag, and `_ask_impl`
  checks it before it looks at the excerpts.

- **`synthesize` reads `ArchivistDigest.transient`.** The field was
  set at three sites in `ground` and read nowhere in the package while
  0.46.0's changelog entry advertised it as delivered. A transient
  digest now returns an inconclusive envelope; a clean miss still
  returns the gap prose.

- **`ArchivistDigest.unserved_scope`.** The collection names a
  `ground` fan-out asked for that the daemon does not serve, in input
  order, and disjoint from `searched_scope`. A digest with an empty
  `searched_scope` and a populated `unserved_scope` searched nothing,
  which is not a statement about the corpus.

- **`lies.qmd.QmdTimeoutError`.** The type that distinguishes a slow
  daemon from a broken one was in neither the import list nor
  `__all__` of `lies.qmd`, so the distinction could not be named
  without reaching into `lies.qmd.cli`.
- **`lies.qmd.integrity.live_index_snapshot` and `snapshots_differ`** —
  read-only snapshot of four aggregates from the live qmd index
  (`collection_names`, `active_doc_count`, `total_vectors`,
  `orphan_vectors`) plus the pure comparator the session guard uses.
  The pre-fix tag-filter guard was blind to ``content_vectors`` writes
  whose backing ``content`` and ``documents`` rows never landed —
  the exact class of write that produced the four live-index orphans
  on 2026-10-03 — because the old snapshot only carried the first
  two aggregates. The four-field snapshot and its mutation test
  pin that class. See
  `.superpowers/sdd/2026-10-01-qmd-daemon-routing/live-index-orphans.md`
  for the root-cause investigation.

- **`tests.fixtures.qmd_bench.decisions` — Task 6's three questions.**
  The fixture's top-level `decisions` block records the per-query
  numbers behind the three open questions the design could not
  close from a single sample: `hyde` stays out (0 of 15 top-1
  changes; the earlier n=1 probe is confirmed at n=15);
  `paraphrase_count = 1` (no authored-paraphrase curve moves a
  failing query into passing rank; latency grows linearly);
  `recall_regression` records today's `lies_gate` 13/15 as the
  reference baseline for the next routing change, with `regressions`
  empty against Task 4's 13/15. The block is fixture data so a
  future reader can diff the numbers without rerunning qmd, and
  `tests/unit/test_bench_fixture.py` pins its shape. Measurement
  harness lives at
  `.superpowers/sdd/2026-10-01-qmd-daemon-routing/measurements/measure_task6.py`;
  full report at
  `.superpowers/sdd/2026-10-01-qmd-daemon-routing/task-6-report.md`.
  Index state at measurement time: 5992 documents, 48992 vector
  rows, 4 orphan hashes (residue from the round-2 investigation,
  none of the 15 fixture queries' expected paths).

### Changed

- **`read_library_bodies` classifies transport errors.** It called
  `client.call_tool` directly while `classify_call_error` lived only
  in `daemon_tool`, so the `except _DAEMON_FAILURES` inside its
  per-path loop was unreachable. Every transport failure became a
  `None` entry, and `read.py` turned that into a `_missing` row: "the
  daemon could not resolve this path". A `ReadTimeout` was
  indistinguishable from a document that is genuinely absent, on the
  one path the batched read exists to make fast. `daemon_tool`'s
  recovery is now `_call_with_recovery(url, name, make_call)` and both
  call sites route through it.

- **`ground` reports the scope it dispatched, not the scope it
  requested.** It bound the unknown set to `_unknown` and returned
  `[]`, which became `no_coverage=True` — a collection set the daemon
  does not serve reached the user as an empty corpus, with
  `searched_scope` naming collections that were never dispatched, and
  nothing logged. The unserved names are logged at warning.

- **`qmd_get` is deleted.** It shelled out for the librarian's
  source-aware read, which the seam moved to the daemon's `get` with
  `lineNumbers: false` — the CLI line-numbers every line
  unconditionally, so `[[slug]]: "verbatim quote"` could not be met
  through it. No callers remained, its docstring still named a
  dispatch that no longer existed, and it raised `QmdCommandError` on
  a timeout where `qmd_query` in the same module raised
  `QmdTimeoutError` carrying qmd's stderr.

- **`lies qmd status` distinguishes an unreadable index from an absent
  one.** Both returned `"index": null`. A WAL database needing
  recovery now reports `{"error": ...}`, because "no index" about an
  index that is right there hides a one-command fix.

- **The `lies qmd` liveness probe no longer leaks an MCP session per
  poll.** `initialize` is not a request but the start of a session: the
  probe read the response and stopped, leaving a live session on a
  machine-global daemon every time. `recycle` polls every 0.5 s for up
  to `ready_timeout`, so one recycle stranded around sixty.

### Fixed

- **A qmd timeout on the batched read is no longer reported as a
  missing document.** The `_missing` entry was built from a transport
  error that never went through the taxonomy.

- **stderr is capped on the wedge paths too.** `_run_qmd` truncated to
  `_MAX_STDERR_BYTES` on the success path only, while
  `search._decode` documented the opposite and renders
  `QmdTimeoutError.stderr` into the user-visible `fallback_reason`. A
  long Node stack trace landed verbatim in the MCP envelope precisely
  when something had gone wrong.

- **`integrity.open_readonly` percent-escapes the path.** A `?` or `#`
  in a path ended the path and started the query string, so the
  connection opened a different file than the caller named.

- **The `lies_gate` tool no longer scores a configuration failure as a
  recall regression.** It read only `result["hits"]`, so an unknown
  collection, a wedge and a timeout all looked like "the document
  ranked low". Faulted queries are excluded from the score and printed
  as `NOT MEASURED`, and the denominator counts the queries actually
  scored rather than every query in the fixture.

- **`_LAZY_LIFECYCLE_ATTRS` was assigned twice** in `cli/qmd.py`, once
  unannotated and once annotated with the same value.

### Removed

- `lies.qmd.cli.qmd_get` — dead since the seam moved the librarian's
  read to the daemon. See "Changed".

### Documentation

- `AGENTS.md` gained a `## Known flakes` section. The `qmd embed` CUDA
  reservation abort is measured, characterised, and left unmitigated,
  and it lived only in a commit message — so the next agent would try
  the same retry, because nothing told them it had been measured and
  found worse.
- `qmd_query`'s docstring claimed the CLI has no collection flag;
  `qmd query --help` documents `-c, --collection <name>`. The
  post-filter stays for a different reason, now the stated one.
- The `ground` exclude is documented as applied before the fan-out
  rather than dropped. `del exclude_expr` is true and reads as
  "inert"; it is enforced, because `ground` resolves the AST into
  `searched_scope_list` first, and the behaviour is now pinned.



## [0.46.0] - 2026-10-03

### Added

- **`ArchivistDigest.transient: bool`** (additive). The grounding archivist
  can now distinguish a process failure from an empty corpus. A
  ``transient=True`` ``ArchivistDigest`` is a slow or failed
  retrieval — the librarian should retry — not "the corpus has
  nothing", which is what ``no_coverage=True`` already meant. The
  two flags are independent: ``no_coverage=False, transient=True``
  is the new envelope on a fan-out dispatch failure. Defaults to
  ``False`` so existing call sites and tests that build a digest
  by keyword remain stable. Pinned by
  ``tests/unit/mcp/test_ground.py::test_archivist_digest_transient_defaults_false``
  and the dispatch-failure envelope tests below.

- **`lies.qmd.access.validate_scope(scope)`** and
  **`lies.qmd.access.qmd_collection_names()`** — the shared seam
  every batched collection filter goes through before reaching the
  daemon. ``qmd_collection_names`` reads the daemon's ``status``
  tool; ``validate_scope`` partitions ``scope`` into the names the
  daemon serves and the names it does not, in input order.
  ``search`` and ``ground`` both call it before issuing a
  ``query``, so a single unresolvable name inside the batched
  ``collections`` array can no longer silently return zero rows
  — a regression from the per-collection fan-out the prior shape
  dropped individually. ``QmdDaemonUnavailable`` /
  ``QmdDaemonWedged`` propagate; an empty ``scope`` returns
  ``([], [])`` with no daemon call. Pinned by
  ``tests/unit/qmd/test_access.py::test_validate_scope_*``
  (five tests: empty input, served/unknown partition, dict-shaped
  status, daemon-down, daemon-wedged).

### Fixed

- **`mcp.search` — `Exception` no longer becomes a corpus claim.**
  The generic ``except Exception`` in ``_search_impl`` previously
  mapped any non-typed exception to ``no_coverage=True`` — a
  false claim about the corpus, for anything outside the
  ``QmdDaemonUnavailable`` / ``QmdDaemonWedged`` taxonomy. The
  handler now sets ``transient=True, no_coverage=False`` with
  ``fallback_reason`` naming the exception, and the unreachable
  ``except QmdCommandError`` branch (which carried the exact
  "qmd unreachable" wording this branch exists to remove) is
  deleted. ``QmdTimeoutError`` keeps its defensive-parity
  envelope. Pinned by
  ``tests/unit/mcp/test_search.py::test_search_unexpected_post_query_failure_returns_transient``
  (renamed from ``...returns_no_coverage``).

- **`mcp.search` — a tag expression that resolves to zero
  collections no longer silently widens to the whole library.**
  ``tag_expr=None`` still widens (the caller did not narrow, so
  every collection is in scope); a non-empty ``tag_expr`` that
  resolves to nothing now reports ``unknown_tags=[tag_expr]`` and
  refuses, rather than answering from the whole library with
  ``unknown_tags == []`` — the shape ``AGENTS.md`` calls out for
  the prompt surface as "a query the user scoped by tag, answered
  from the whole library, with nothing in the response saying
  so". Pinned by
  ``tests/unit/mcp/test_search.py::test_search_tag_only_filter_is_not_silently_widened``.

- **`qmd.access.classify_call_error` — `LocalProtocolError` is a
  passthrough, not a recycle-retry.** The docstring and
  ``AGENTS.md`` "Deliberate taxonomy gap" paragraph both said
  ``HTTPStatusError`` and ``RemoteProtocolError`` were
  passthrough — wrong on the second half (measured against
  installed httpx 0.28.1: ``RemoteProtocolError`` classifies as
  ``recycle-retry`` because it inherits ``ProtocolError →
  TransportError``) and silent on a third: ``LocalProtocolError``
  classifies the same way but a retry against a malformed
  request sends the same bytes back to fail the same way, and a
  recycle kills in-flight work belonging to other clients of a
  machine-global daemon. ``LocalProtocolError`` joins an explicit
  passthrough set; ``RemoteProtocolError`` keeps
  ``recycle-retry`` (the daemon's response was malformed, which
  is server-side state). All three classes pinned in
  ``tests/unit/qmd/test_access.py``, including a
  wrapped-by-fastmcp case for ``LocalProtocolError`` so the
  ``__cause__`` chain walk cannot silently re-merge the
  client-side case.

- **`qmd.access._recycle_data_dir` — six unreachable lines past
  the ``return`` removed.** A copy-paste artifact from the
  recycle rewrite.

- **`mcp.grounding` — fan-out dispatch failures now log and
  surface as `transient=True, no_coverage=False`.** Three
  ``except Exception`` handlers (the unscoped fan-out, the tagged
  fan-out, and the legacy librarian dispatch) previously folded
  any process failure into ``no_coverage=True`` and emitted one
  ``warnings.warn`` line — which the default filter prints once
  per location, so a persistently failing daemon goes quiet. The
  three handlers now log through ``_log.error`` and return an
  ``ArchivistDigest`` with ``transient=True, no_coverage=False``.
  Pinned by
  ``tests/unit/mcp/test_ground.py::test_ground_fanout_dispatch_failure_surfaces_as_transient``
  and the tagged-path version, so the unscoped and tagged
  fast-paths cannot drift on the failure envelope.

## [0.45.1] - 2026-10-03

### Fixed

- **`mcp.search._post_query` — the `timeout` parameter is no longer dead.**
  The seam (`lies.qmd.access.daemon_tool`) now accepts a per-call
  `timeout=` keyword and forwards it to `fastmcp.Client.call_tool`
  as the per-request MCP read deadline. `_post_query` threads
  `_current_timeout()` through, so a `LIES_QMD_FANOUT_TIMEOUT` change
  takes effect on the next daemon call instead of waiting for the
  cached httpx client to be invalidated by a `LIES_QMD_URL` change.
  The previous shape implied a guarantee the code did not provide;
  the CLI path read its deadline per call and the daemon path did
  not. Both paths now agree. The cached client's cache key is the
  URL only — the deadline is the per-call argument, not part of
  the key. Pinned by
  `tests/unit/qmd/test_access.py::test_a_per_call_timeout_is_forwarded_to_call_tool`
  and friends.

- **`mcp.grounding._fanout_collections` — route through the qmd access seam.**
  The previous shape was a per-collection CLI subprocess fan-out
  with a consecutive-error counter that fired a manual `recycle()`
  on threshold. The seam's typed errors (`QmdDaemonUnavailable`,
  `QmdDaemonWedged`) now reach the archivist unchanged — a
  process failure is no longer folded into `no_coverage=True`,
  which would be a false claim about the corpus. The fan-out is
  one daemon `query` against the resolved collection list with
  the daemon's `collections` push-down; the push-down is exact,
  so one round trip replaces N. The pre-#106 VRAM rationale (per-
  collection subprocess fan-out spiked the embedding model under
  OR-scoped queries) is gone with the per-collection loop, and
  the corresponding `AGENTS.md` paragraph is rewritten to name
  the daemon path. Pinned by
  `tests/unit/mcp/test_ground.py::test_fanout_unscoped_routes_through_the_daemon_seam`
  and the typed-error propagation tests.

- **Stale orphan counts in `qmd.integrity.index_orphans` docstring.**
  The 2026-10-01 probe baseline (1566 / 41332) and the 2026-10-03
  post-cleanup reading (4 / 4) are now both labeled as dated
  measurements rather than quoting the older number as the live
  state. The probe numbers remain in `tests/fixtures/qmd_bench.json`
  with the full historical context (re-baselined 2026-10-02 with
  two quality-bit-identical runs).

- **Stale tool-name vocabulary in `qmd.mcp_fallback` module docstring.**
  The fallback's tool surface (only `wiki_search` and `wiki_read`
  for the agent's retrieval) is now described in the seam's
  vocabulary instead of dangling references to `qmd_query` /
  `qmd_get` / `qmd_status` / `qmd_update` that the seam does not
  re-implement.

- **Stale `QmdTimeoutError` narrative in `mcp.search._post_query`**
  — the docstring's "A timeout on the daemon side surfaces as
  `QmdTimeoutError`" was the pre-#106 CLI behaviour. The seam
  raises `QmdDaemonWedged` (recycle-and-raise) for a daemon-side
  timeout, and `QmdTimeoutError` is a CLI-only class. The
  `except QmdTimeoutError` clauses are kept as defensive
  fallbacks for a future CLI re-route, with the docstring
  updated to say so.

- **Stale framing in `tests/unit/qmd/test_lock.py`**
  — three tests at lines 32-62 assert on the module constants
  `_LOCK_PATH` / `_PID_PATH` / `_STATE_PATH`, which are frozen
  at import. The acquire/release path now resolves per
  acquisition via `_lock_paths()`, so the constants are no
  longer on the locking hot path. They are still used by
  `_register_holder` for the heartbeat siblings, and the test
  docstring is updated to name the constant's *real* use
  (the heartbeat writers), so a future reader does not assume
  the constants are the live lock path.

## [0.45.0] - 2026-10-03

### Fixed

- **The live-index guard missed the orphan-write class.** The
  tag-filter session guard snapshot carried only two aggregates —
  `store_collections` names and active document count. The four
  live-index `content_vectors` rows observed at 2026-10-03 03:50
  PDT belong to a class a two-field snapshot cannot see:
  `content_vectors` writes whose backing `content` and `documents`
  rows never landed, or were later hard-deleted by an intervening
  `qmd collection remove`. The pre-fix guard returned `equal=True`
  on that write and the orphan rows slipped past.

  The fix is a four-field snapshot (`collection_names`,
  `active_doc_count`, `total_vectors`, `orphan_vectors`); the
  fourth is the load-bearing one. The comparator is now a pure
  function, `lies.qmd.integrity.snapshots_differ`, so the unit
  tests do not need the integration harness.

### Added

- **`lies.qmd.integrity.live_index_snapshot`** — the read-only
  four-aggregate snapshot the guard compares, exposed so the guard
  and any other consumer read the same numbers.

### Changed

- **The bench fixture records the three questions a single sample
  could not close.** `hyde` stays out (0 of 15 top-1 changes at
  n=15), `paraphrase_count = 1` (no authored-paraphrase curve moves
  a failing query into passing rank; latency grows linearly), and
  `recall_regression` records today's `lies_gate` 13/15 as the
  reference for the next routing change. The block is fixture data,
  pinned by `tests/unit/test_bench_fixture.py`, so a future reader
  can diff the numbers without rerunning qmd.


## [0.44.0] - 2026-10-03

### Added

- **`lies.qmd.integrity` — read-only SQLite inspection of the qmd index.**
  qmd has no read-only open mode, and the index has no `content_vectors`
  → `content` foreign key, so vector rows can outlive the documents they
  belong to and nothing cascades them. LIES now reads the index directly
  via `file:$XDG_CACHE_HOME/qmd/index.sqlite?mode=ro` rather than shelling
  out to qmd for diagnosis — every entry point opens read-only, with
  `open_readonly(db)` as the module's only connection constructor.
  Three named functions: `index_orphans(db) -> OrphanReport` (counts
  vector rows whose hash has no backing `content`), `is_embedded(db, h)
  -> bool` (the per-document semantic-search reachability check), and
  `collection_drift(db) -> dict[str, list[str]]` (registered paths
  that no longer exist on disk). An `integrity_summary(db)` aggregator
  composes the three with three coverage queries (`documents_total`,
  `documents_active`, `documents_active_without_vectors`) for the
  status command. `lies qmd status` now prints this as an `index` block
  alongside the daemon fields; `null` when the index is absent.

## [0.43.3] - 2026-10-01

> Version numbering: this branch has no `0.43.2` entry because nothing was
> released as 0.43.2 — the last tag is `v0.40.0`. The release-scaffold
> bump was renumbered 0.43.2 → 0.43.3 when the fastmcp-floor change landed
> alongside it. Nothing was deleted; no 0.43.2 record ever shipped.

### Fixed

- **`read` returned bodies that could not be quoted.** F19's citation
  contract is `[[slug]]: "verbatim quote from the cited span"`, and the
  library branch answered with the CLI's `qmd get` output: every line
  prefixed `N: `, behind a `qmd://path  #docid` header that
  `--no-line-numbers` does not remove. The branch now issues the daemon's
  `get` with `lineNumbers: false`, which is the only source of clean
  text in qmd, and reads the body from the content block rather than
  `result.data` — qmd answers with an EmbeddedResource block, so a
  `.data` read stores `""` for a document with content.
- **A down daemon was reported as "all reads failed".** The library
  branch wrapped every call in `except Exception: log.warning(...)` and
  skipped, so `QmdDaemonUnavailable` was swallowed, every path was
  skipped, and the batch surfaced as `ToolError("all reads failed")` —
  a claim about the corpus that is really a claim about the process. A
  daemon that is down or wedged now re-raises; a document qmd cannot
  resolve is still logged and skipped with its siblings intact.
- **`read` raised from inside a running event loop.** The sync tool
  bridges the async seam, and `asyncio.run` from a thread that already
  has a loop raises `RuntimeError` — the bug `ground()` shipped with in
  #106. The bridge now runs the coroutine on its own thread and loop
  when one is already present.
- **The default qmd daemon URL 404'd.** `DEFAULT_QMD_URL` was
  `http://127.0.0.1:8181`; qmd's HTTP MCP server serves exactly one
  route, `/mcp`, which `qmd.lifecycle` has always included. Every call
  at the default reached a live daemon, got a 404, and was classified
  and reported as a *down* daemon — with "start it with `lies qmd up`"
  as the advice, for a daemon that was already running.
- **One bad page discarded every good body in a `read` batch.** When the
  daemon answered a request with notices but no document block, the
  extraction ran *outside* the per-path `try`; the exception propagated
  and the partially-filled result was lost, so a single anomalous
  document silently cost the caller every other body it had asked for.
  Both spellings of "no body for this path" — the call raised, or the
  call succeeded with nothing usable — are now treated identically:
  logged, skipped, siblings intact. Which channel qmd picks is an
  implementation detail of its error signalling, and a batch's outcome
  must not depend on it. `ToolError("all reads failed")` remains the
  loud failure, raised once when the batch genuinely produced nothing.
- **Every qmd subprocess now runs with `NO_COLOR=1`.** Policy, not a
  fix: qmd's only `NO_COLOR` consumer is
  `const useColor = !NO_COLOR && process.stdout.isTTY`, and LIES always
  pipes, so colour is already off and the override cannot change
  today's output. It is defence in depth — it removes colour as a
  variable, and forcing rather than inheriting means an operator who
  exported `NO_COLOR=0` for their own terminal cannot change what
  `qmd_query` parses. An earlier draft of this entry claimed the
  override suppressed a `⠋ Gathering information` spinner that qmd
  writes to stdout, and that is wrong: qmd 2.5.3 has no such spinner
  (its only cursor control writes to stderr). The spinner is real but
  belongs to `ipull`, a transitive dependency of `node-llama-cpp`,
  writing through `stdout-update` to stdout with no TTY guard — and it
  only runs while a model is downloading into a cold cache.
  `NO_COLOR` does not suppress it; the lever is a warm model cache.
- **`fastmcp>=2.0` advertised compatibility the code does not have.**
  `Client.call_tool(..., raise_on_error=False)` is keyword-only on
  FastMCP 4 and absent on 2.x, and the access seam calls it on every
  daemon call, so a 2.x install would die with a `TypeError` at the
  first call rather than at install time. The floor is now `>=4.0`.
- **`qmd embed` and `qmd update` were killed as wedges while they were
  working.** The wedge detector fires after 30s of silence, which is a
  good default for an interactive query — but both of these are silent
  for their *entire* duration under a pipe. `qmd embed` writes exactly
  one byte (a stderr spinner escape) and then nothing while the model
  loads and runs; `qmd update` writes nothing at all, its progress being
  a stderr write behind an `isTTY` check. Measured on a cold cache, one
  tiny document takes 9.4s of unbroken silence, and four collections
  under host contention crossed the bound and were killed mid-progress.
  Both now pass an idle bound of `timeout * 0.5`. Half rather than all:
  the loop checks the total bound first, so an idle bound equal to the
  total could never fire and every kill would lose its `last_output`
  diagnostic. The cost is a wedged command holding `with_qmd_lock()` for
  up to half its total bound instead of 30s — stated, not hidden, and
  still the better trade against killing healthy work. Retrieval
  commands keep the 30s default: a query silent for 30s genuinely is
  wedged.
- **`qmd query --json` failed on a cold model cache.** When qmd's models
  are not yet cached it downloads them, and that download is driven by
  `ipull` (a transitive dependency of `node-llama-cpp`) through
  `stdout-update`, whose `UpdateManager` defaults to `process.stdout`
  and writes there with no TTY guard and no `NO_COLOR` check. Under a
  pipe that progress lands in front of the JSON, and `json.loads` failed
  at char 0 — reported as `qmd query returned invalid JSON`, on exactly
  the runs with a cold cache, which is why it read as intermittent.
  `_parse_json_list` now finds the JSON rather than demanding the stream
  begin with it, and still rejects genuinely malformed output — quoting
  what actually arrived when it does. Covered by four tests, one of which
  feeds the real ipull byte sequence (`\x1b[?25l⠋ Gathering information…`)
  and asserts the JSON is still recovered, and three of which assert that
  garbage, truncated output, an empty stream and a JSON *object* are all
  still rejected. A test that reproduced this against the live binary was
  written and deliberately not shipped: forcing a cold cache makes qmd
  download ~1.2 GB of models per run.

### Changed

- **Both qmd URL defaults now source `config.DEFAULT_QMD_URL`.** The
  same bare-origin `http://127.0.0.1:8181` was written out three times,
  and only the config one was corrected. The two class defaults are not
  reachable today — the sole production construction site
  (`orchestrator.py`) passes `get_qmd_url()` explicitly, and
  `QmdMcpClient` has no production construction site at all — so nothing
  was broken by them; but any future caller omitting `url=` would have
  reproduced the 404-as-down-daemon misdiagnosis. Sourcing the constant
  closes the class rather than the three instances.
  `QmdMcpClient.url` therefore also changes host `localhost` →
  `127.0.0.1` by inheriting the constant. Inert today — the class has no
  production construction site — but it is a behaviour change and is
  recorded as one.

## [0.43.2] - 2026-10-02

### Fixed

- **`read` returns verbatim bodies.** The CLI's `qmd get`
  line-numbers every line and keeps its `qmd://path  #docid` header
  even with `--no-line-numbers`, so every body the read tool
  returned carried provenance F19 cannot quote. A citation is
  `[[slug]]: "verbatim quote from the cited span"]`; against
  `claude_code/plugins.md` the old path produced 493 numbered lines
  behind a `qmd://claude_code/plugins.md  #f4a9c5` header. The
  daemon's `get` with `lineNumbers: false` is the only source of
  clean text in qmd, and it returns the same document as 492 lines
  with zero numbered prefixes.

  The daemon answers with an `EmbeddedResource` content block
  rather than a string, so `result.data` is `None` and the text
  lives one hop down at `content[].resource.text`; the read tool
  reads it there.

### Changed

- **`fastmcp>=4.0` is now a hard floor.** `access.py` calls
  `client.call_tool(name, arguments, raise_on_error=False)` on every
  daemon call, and that keyword is keyword-only on FastMCP 4 and
  absent on 2.x — so a 2.x install died with a `TypeError` at the
  first qmd call rather than at install time. 4 also supplies
  `fastmcp.server.transforms.PromptsAsTools`, which `server.py`
  imports directly.


## [0.43.1] - 2026-10-01

### Added

- **`lies.qmd.access` — the seam every qmd call goes through.** One
  module owns the transport decision, so no call site picks one:
  `DAEMON_TOOLS` (`query`, `get`, `multi_get`, `status`) and
  `CLI_ONLY_OPS` (index maintenance, the collection and daemon
  lifecycle verbs, `ls`/`doctor`/`bench`, and BM25 `search`, which the
  daemon has no path for). `daemon_tool(name, arguments)` makes one
  daemon call; `classify_call_error(exc)` answers
  `(action, retryable)` for any qmd call and now has a second caller
  outside the seam — the pydantic-ai `QmdRecycleToolset` — so the
  agent path and the library path cannot disagree about what a wedge is.
- **`QmdDaemonUnavailable`** and **`QmdDaemonWedged`**, the two daemon
  failure modes as distinct types. A down daemon raises the first,
  naming both `lies qmd up` and `LIES_QMD_URL`; there is no
  availability-based fallback, no degraded tag, and no silently-empty
  result. A wedged one raises the second, carrying `last_output` — the
  tail of qmd's own daemon log, read *before* the recycle so it is the
  wedged daemon's log and not the replacement's (qmd truncates
  `mcp.log` on every start).

### Fixed

- **Every HTTP daemon call failed at connect with a `TypeError`.**
  fastmcp invokes the httpx client factory with `follow_redirects=`,
  which the shipped `_build_qmd_httpx_client` did not accept, so both
  the agent toolset and any HTTP call through the seam raised before
  reaching the recycle taxonomy. The factory now accepts the keyword and
  builds its client from the httpx generation fastmcp actually installed.
- **The recycle taxonomy matched nothing fastmcp raises.** fastmcp 4
  vendors its own httpx as `httpx2`, and `httpx2.ReadTimeout` is not a
  subclass of `httpx.ReadTimeout`; a dead session additionally arrives
  wrapped in a bare `RuntimeError` with the real transport error on
  `__cause__`. Every wedge therefore classified as a passthrough, so no
  recycle ever ran. `classify_call_error` now matches both httpx
  generations by exception class name and follows the cause chain
  fastmcp's wrapper documents, and treats a `CONNECTION_CLOSED`
  `MCPError` as the wedge it is. On the agent path the first call is now
  caught broadly for the same reason; the *retry* still re-raises
  anything the classifier does not own, so a decode error is no longer
  reported to the model as the daemon being unreachable.
- **The daemon read timeout was a second literal.** The HTTP factory
  hardcoded `read=60.0` while the CLI paths read
  `get_qmd_query_timeout()`, so the same retrieval could get two
  different deadlines depending on the transport. Both now read the one
  getter.

## [0.43.0] - 2026-10-01

### Added

- **`transient` on the `search` envelope.** A qmd call that outlives
  its deadline is a fact about the *run*, not the corpus, and the two
  were conflated. `no_coverage` keeps its meaning ("this search found
  nothing"); `transient` is new and true only when the search did not
  finish. A caller can now distinguish *the library has nothing* from
  *the lookup did not complete* without parsing prose.
- **`QmdTimeoutError`**, a subclass of `QmdCommandError`, carrying
  qmd's captured `stderr`. The deadline message is a constant, so a
  timeout previously arrived with no indication of whether the time
  went into query expansion, embedding, or reranking.
- **One deadline for every qmd retrieval call site.**
  `lies.config.get_qmd_query_timeout()` (default 60s, override
  `LIES_QMD_FANOUT_TIMEOUT`) is now the single source, read at call
  time by both the grounding fan-out and the `search` tool. Wiring
  `search` to the variable alone left the fan-out on its own 15s
  default, so one env var had two answers for the same subprocess;
  the shared getter removes the second place to change.
  `LIES_QMD_FANOUT_TIMEOUT` keeps its historical name because it has
  been the fan-out's override since 0.40.0. A malformed or
  non-positive value falls back to the default rather than raising —
  an operator typo should cost the default budget, not the search.

### Changed

- **A qmd timeout is no longer reported as `qmd unreachable`.** The
  label claimed a connection failure; a timeout means the daemon was
  there and slow. Real `QmdCommandError`s (non-zero exit, malformed
  output) keep the old wording — the two failures stay
  distinguishable.
- **The per-call search deadline is 60s, up from 15s.** The 15 was
  inherited by copy from `grounding._QMD_FANOUT_TIMEOUT`, sized on a
  2026-09-25 cold-daemon probe (~3-7s warm, cold rerank can pass
  10s) and appropriate to a 14-collection fan-out where one stall is
  15s out of a 210s ceiling. `search` is a single call, so the same
  number was the whole operation. Measured warm latency against the
  5987-document corpus (2026-10-01) is 5.6-6.0s; 60s is
  `qmd_query`'s own default, so this is no longer stricter than the
  layer beneath it. Liveness probes in `qmd.lifecycle` and
  `qmd.daemon` are deliberately excluded and keep their own 15s/5s
  deadlines (`DAEMON_START_TIMEOUT_S`, `PROBE_TIMEOUT_S`,
  `STATUS_TIMEOUT_S`): a slow answer to "is this alive?" is itself the
  failure, and a wedged daemon should be reported rather than waited
  on.

### Fixed

- **A slow search no longer reaches the user as "No relevant content
  found in library."** The librarian contract instructs the model that
  `no_coverage=True` means the corpus has zero hits for the question;
  a timeout set that flag, so an intermittent 15s stall asserted that
  the library contained nothing on the topic — for a query that
  returned in under six seconds on the retry. The librarian contract
  now tells the model to retry once on `transient` and, if it stays
  transient, to report the lookup as inconclusive rather than as
  missing content.

## [0.42.0] - 2026-09-29

### Breaking changes

**MCP prompt arguments collapsed to a single string tail.** Five prompts
took typed parameters past position one; hosts that bind slashes to MCP
prompts pre-tokenize the tail on whitespace and bind tokens
positionally, so a bare word landed in a `bool` / `int` / `list[str]`
slot and FastMCP rejected it with a JSON-parse error. Every prompt now
takes exactly one `str` that consumes the whole tail, and parses its
flags internally.

| Prompt | Before | After |
|---|---|---|
| `ask` | `question: str, tag_expr: str \| None = None, exclude_tags: list[str] \| None = None` | `tail: str` — the `+tag` / `-tag` filter atoms are parsed out of the question text instead of passed as arguments |
| `ground` | `question: str, top_k: int = 3` | `tail: str` — `--top_k N` from the leading run |
| `collections` | `subcommand: str, args: list[str] \| None` | `tail: str` — `<subcommand> <args…>` |
| `ingest` | `source: str, delete_slug=None, batch_dir=None, dry_run: bool` | `tail: str` — `<source>` / `--batch <dir>`, plus `--collection`, `--slug`, `--title`, `--slug-prefix`, `--exclude-stem`, `--exclude-dir`, `--force`, `--dry-run` |
| `lint` | `check: str \| None, fix: bool` | `tail: str` — `<check>` / `--check <name>`, `--name <wiki>`, `--fix`, `--force-repair` |
| `reindex` | 5 × `bool` | `tail: str` — `--reconcile`, `--embed`, `--force`, `--cleanup`, `--all`, `--name <wiki>` |
| `sync` | `collections: list[str] \| None, no_ingest/force/dry_run: bool, jobs: int, scraper_timeout: int` | `tail: str` — the request verbatim, plus `--source`, `--name`, `--force`, `--wait`, `--fail-busy`, `--wizard`, `--skip-reindex` |

`ask` is renamed to `tail` along with the other six, and the rename is
load-bearing rather than cosmetic. An earlier draft of this release kept
`question`, which made `ask` the *only* prompt where a retired call
failed silently: FastMCP filters `get_prompt` arguments to the declared
signature, so
`get_prompt(name="ask", arguments={"question": …, "tag_expr": …})`
rendered a well-formed body that searched with `tag_expr=None` and
**dropped the filter with no warning** — a query the user scoped by tag,
answered from the whole library, with nothing in the response to say so.
The other six raise `Missing required arguments: {'tail'}`. Naming the
parameter `tail` makes `ask` fail the same loud way. The filters move
into the tail as `+tag` / `-tag` atoms, which is what the ask plugin's
slash convention always used.

Programmatic callers replace the keyword arguments with one string:
`get_prompt(name="lint", arguments={"check": "orphans", "fix": true})`
becomes `get_prompt(name="lint", arguments={"tail": "--check orphans --fix"})`.

`sync` no longer parses collection names positionally. Its tail is a
*request*, handed to the agent verbatim in the same shape `ask` and
`ground` use: the agent identifies the collections the request names,
grounds an ambiguous name against `collections_read`, and asks the
user before dispatching. A request naming no collection leaves the
positional off, which is how `lies sync` covers every collection with
a scraper — so `all` and `--all` are no longer special-cased. Flags
stay deterministic: the body parses them and threads them into each
call, so `--skip-reindex` is never left to the model to spot.

The positional parse had a defect class of its own. It read one word
per collection, so `sync please resync my library collections` rendered
six `Bash(lies sync <word>)` invocations — a scrape-and-reindex chain
per word, none of them on the collection the user meant. A five-name
cap refused the tail, which fixed the paste accident and broke the
legitimate six-collection request. Both halves are gone: the model
reads the sentence instead of the parser counting words. No forwarder,
no alias, no deprecation path.

The version is **minor** (0.42.0). The prompt surface is host
configuration rather than a versioned programmatic API: the only
consumers are an MCP host's slash binding and the routing rules in
`instructions.md`, and 0.41 shipped the same seven names. A major bump
is reserved for the six *tools* (`search` / `read` / `lib_ask` /
`collections_read` / `lint` / `reindex`), which are the surface a
programmatic client depends on.

### Added

- 7 slash-command MCP prompts: `ask`, `collections`, `ingest`, `lint`,
  `reindex`, `sync`, `ground`. Each returns a single user-role
  Message that templates a routed tool call against the LIES surface.
  Hosts bind the names under their server prefix (e.g. `/lies:ask`).
- `list_prompts` and `get_prompt` tools via the FastMCP
  `PromptsAsTools` transform. Tool-call arguments are not subject to
  the host's slash pre-tokenization, so a full multi-word question
  reaches the prompt body parser intact — including the `+tag` / `-tag`
  filter tokens, which the prompt body strips and routes into
  `tag_expr` / `exclude_tags` on the dispatched `search` / `read` /
  `lib_ask` calls. The 7 `@mcp.prompt` registrations are unchanged;
  this adds a second path to the same functions. Hosts that bind
  slashes to MCP prompts pre-tokenize the slash tail on whitespace, so
  the slash path still reaches a single-parameter prompt with only its
  first token.
- Server-emitted routing rules in `instructions.md`: a user message
  carrying `+tag` / `-tag` filter tokens routes through `get_prompt`,
  not `lib_ask` directly, because `lib_ask` is the synthesizer inside
  the `ask` prompt's body rather than a user entry point.
- `check` parameter on the `lint` tool, narrowing the report — and the
  repair, when `fix=True` — to one finding category. Matched
  case-insensitively against the categories actually present in the
  report, with an optional plural (`orphans` and `orphan` both select
  `orphan`). A `check` that matches nothing renders the report with the
  available categories listed rather than an empty report that reads
  like a clean wiki. The `lint` prompt's `--check` flag routes to it;
  previously the prompt rendered a `check=` argument the tool did not
  have. A blank `--check=` now refuses rather than running unfiltered:
  the tool reads an empty check as *no filter*, so the call would have
  returned the full report for a scoped request. The first repair
  appended a note to the live call, which is the weaker answer — a body
  that renders a command is a body promising an answer, so the user
  had to read a trailing sentence to learn the scope was gone.
- `--check <category>` on `lies lint`, so the CLI carries the same
  scoping the MCP tool has. The prompt documented a filter the CLI
  could not run, which read as a broken flag rather than a missing one.

### Changed

- MCP tool `ask` renamed to `lib_ask`. The librarian+synthesizer
  pipeline is unchanged; only the wire name and Python symbol
  changed so the slash slot and tool surface stay de-duplicated.
- The per-test budget gate re-measures in isolation before failing. A
  full-suite run measures each test under contention, and the 0.15s
  line sits close enough to the noise floor that a test whose body is
  instantaneous gets flagged for scheduler latency rather than for
  cost. An in-suite breach is now re-run on its own; only a test that
  breaches there fails the run. This replaces the previous remedy for
  a false positive — a `@pytest.mark.slow` mark, which removes the
  test from the default run rather than fixing anything. The series
  added 21 slow marks and removed 30, a net of −9 (173 on `main` before
  it, 164 after), and the gate threshold is unchanged at 0.15s. Every
  path where the re-measure cannot be performed now names itself on the
  terminal, so a harness that stopped working is distinguishable from a
  re-run that genuinely cleared the limit — the two were previously the
  same empty dict, and the gate could not explain its own verdict.
- **A budget-gate failure that is not a timing failure no longer reads
  as one.** When the isolation re-measure could not be performed — a
  test that errors in a fresh process, a broken import, a flake, a
  timeout — the gate fell through to the timing report anyway: "HARD
  LIMIT VIOLATIONS ... confirmed in isolation" plus a rubric about
  making tests cheaper, for a test that had in fact crashed. The
  re-run's real output appeared on one line above the rubric, and on
  the *last stdout line* only, while a collection error prints to
  stderr — so the one line a reader needed was usually the one line
  dropped. A bail is now its own outcome, not an empty measurement: the
  gate quotes both streams, says plainly that it holds no measurement,
  and still fails closed. Only a re-run that happened and said "slow"
  produces the timing report.
- **The budget gate's isolation re-run no longer has one fixed
  timeout.** A 120s bound on the whole batch is wrong at both ends: high
  enough to cover any realistic batch it is also high enough that one
  wedged test stalls the gate for two minutes, and low enough for a
  typical batch it fires on a loaded machine and converts a noise
  verdict into a hard failure — the exact outcome the re-measure exists
  to prevent. The bound is now a 60s floor plus 5s per test in the
  batch, and a test reads the value back off the `subprocess.run` call
  rather than only checking the arithmetic.
- `sync` renders one command per named collection. `lies sync` takes a
  single positional, so `sync pydantic opencode` is two invocations
  rather than a list-valued flag the CLI has no such option for.
- `ingest` asks rather than rendering a placeholder. With neither
  `--source` nor `--batch` the old body emitted
  `--source '<source>'`; the body now asks which file to ingest.

### Fixed

- **Every `Bash(...)` the `ingest` and `sync` prompts render was a
  command the CLI rejects.** The two prompts whose job is mutating the
  library emitted `lies ingest|sync --data-dir "$LIES_DATA" …`, and
  `--data-dir` is not an option on any `lies` command — every one of
  those commands exited 2 before doing anything. The vocabularies were
  transcribed from hand rather than from the Typer signatures, and
  invented `--only`, `--jobs`, `--scraper-timeout`, `--no-ingest`,
  `--dry-run`, `--type`, and `--delete` alongside it. Each prompt's
  flag set is now the option set its target command declares; a flag
  the command does not have is reported by name instead of rendered.
  `tests/unit/mcp/test_rendered_commands_are_runnable.py` checks every
  rendered command against the live Typer app, so a hand-transcribed
  table that drifts turns a test red instead of shipping a broken
  command.
- **`ingest --delete <slug>` no longer renders a ghost verb.** Nothing
  in the CLI removes an ingested page. The body now names the two
  things that do exist — `lies library delete` removes a collection's
  `config.yaml`, and removing a page file is a filesystem delete — and
  asks which was meant.
- **`ingest` no longer drops a flag the CLI accepts.** `--collection`,
  `--slug`, `--title`, `--slug-prefix`, `--exclude-stem`, and
  `--exclude-dir` were parsed and then thrown away with no note, so a
  user who typed them correctly got a command missing them. The
  vocabulary is now per-command, and every flag it admits is rendered.
- **`collections` no longer drops `--tag` on `new`, `--json` on
  `list` / `bootstrap-all`, or `--force` on `delete`.** All four are
  declared by `lies library`; the prompt's union vocabulary did not
  match, and `--json` was declared a *value* flag while being read as
  a boolean, so the correct spelling produced both the right command
  and a false "needs a value" alarm. The vocabulary is now per-verb.
- **A repeatable flag no longer loses its earlier values.**
  `lies library modify --tag a --tag b` is a real shape — `--tag` is
  declared "Tag to add (repeatable)" — and a last-wins dict dropped
  `a` with no word. `TailParse.repeats_of()` returns every occurrence
  in order; `values` keeps the scalar reading. The backing field is
  private (`_repeats`) so the accessor is the only read path.
- **`sync` no longer splices an unrecognized flag's value into a
  collection name.** `sync --jbos 8` rendered `--only 8`: the typo'd
  flag's value became a collection nobody named, and the same body said
  "no command was run" a sentence after printing `Run Bash(lies sync
  8)`. All seven prompt bodies now call one shared guard
  (`_refuse_unless_clean`) that refuses on a missing flag value or on a
  flag whose value would be re-read as an argument, and `note()` no
  longer claims a command was not run — a body that appends `note()` is
  by definition rendering one.
- **`ground` no longer deletes flag-shaped words from the question.**
  Running the full flag grammar over a question that is mostly prose
  turned "what is the `--only` flag" into a search for "what is the
  flag" — the same defect the filter parser guards against, one layer
  up. Flags are now read from the leading run only, `--` terminates
  them, and a body that grounds a question containing a flag says so
  rather than leaving the agent to guess why the words came back.
- **`run_lint(check=…)` no longer narrows the `log.md` entry.** The
  persisted `lint-report.md` always received the full merged report,
  but the wiki's audit log was titled from the filtered one, so a
  scoped run read as "2 findings" for a merge that carried twenty
  across five categories. The entry now counts the merge and states
  the narrowing: `check=stale, 1/4 matched`.
- **The `check` filter trailer counted the wrong denominator, and the
  lint prompt's `check` leaked the full report.**
  `len(shell) + len(llm)` overstates the merged total, because
  `merge_lint_reports` dedups on `(category, pages, message)` and the
  two sources overlap on every mechanical category. Separately,
  `_filter_lint_report` copied the *unfiltered* `report_markdown` onto
  a report whose `findings` said otherwise; anything rendering the
  markdown directly ignored the filter.
- **The parse-problem note cannot weld onto the command.** `note()`
  returned a bare sentence, so a body appending it to a rendered
  command produced `…--forceUnrecognized flag(s) ignored: --bogus.` It
  now returns `""` or a leading-space-prefixed sentence.
- **The `ingest` and `collections` bodies no longer run a command with
  a placeholder in it.** A missing slug rendered
  `lies library modify <slug>`, which the agent would then run. The
  `collections` verbs that require a name (`show`, `where`, `new`,
  `modify`, `tag`, `delete`) now refuse with the name they need, which
  also removes the literal `'<slug>'` / `'<name>'` from every rendered
  `Bash(...)` line.
- **`collections` no longer lets an unknown flag's value become the
  collection name.** `show --tag cli` parses as an unknown flag plus
  the positional `cli`, because `show` declares no `--tag`; the body
  rendered `collections_read(name='cli')` while its note said the flag
  was ignored, so the flag the user typed decided which collection got
  queried. `TailParse.repurposed` records the pair, and a body that
  consumes positionals asks instead of rendering. A *declared* boolean
  followed by a bare word stays a surplus positional, reported by the
  existing leftover note.
- **`tag <slug> --tag <value>` no longer drops the tag.** The `tag`
  branch read tags from the positionals only, so the flag spelling
  rendered `lies library modify <slug>` and ran with no tags at all.
  Both spellings now reach the command.
- **`--no-skip-reindex` is no longer silently discarded by `sync`.**
  The boolean table carried the negated spelling of every paired flag
  except this one, so the flag was dropped with no word and the
  rendered command ran the qmd `update` + `embed` chain the user asked
  to skip.
- **The runnable-command test reads `secondary_opts`.** Typer keeps the
  `--no-x` half of a `--x/--no-x` boolean out of `param.opts`, so the
  test rejected `lies sync pydantic --no-wait` — a flag the CLI
  declares and the prompt renders. The failure pointed at the table,
  and the tempting repair was to delete a working flag from it.
- **The runnable-command test is compared against the live Typer app in
  both directions.** Its first version generated its tails *from the
  hand-written vocabulary tables*, so it could only ever catch a table
  that gained an invented flag; a flag the command declares and the
  table omits produced no row and passed. `_check_command` also
  validated flag names and an arity ceiling only, so it accepted
  `lies library new` — which exits 2 for a missing slug. Both gaps
  were found by mutating the implementation and watching the suite
  stay green. The test now asserts *equality* between each table and
  the options the live app declares, checks that every declared flag
  actually reaches the rendered command (a per-body render tuple is a
  fourth hand-written table, and a flag dropped from it was silent),
  and rejects a value-taking option with no token after it and a
  required argument with no positional.

  Two runtime preconditions sit below the signature and stay outside
  any introspection: `lies library new` raises
  `BadParameter("library new requires --source")` in its body, and
  `lies ingest --source` exits 2 without `--collection` or
  `--slug-prefix`. Both are the *default* invocation of their verb, and
  both are now body-level refusals with their own regression tests.
- **`--title` no longer silently eats the source, and `ground` no
  longer swallows a bad `--top_k`.** `--title` takes every word up to
  the next flag, so `ingest --title "Pydantic basics" x.md` reported
  "no source given" and pointed away from the cause; the body now
  states the ordering rule wherever `--title` is set. `--top_k=abc`
  fell back to 3 in silence, and `--top_k=99` clamped to 10 unremarked;
  both are named now. The `ingest` prompt description also no longer
  advertises a `--delete slug` the body says does not exist.
- **The `ask` and `ground` bodies render the question as a fenced
  verbatim block** rather than a `repr()` literal. A question
  containing a quote or a newline arrived at the tool call carrying
  escape sequences the agent had to know to strip, and a tail
  containing a closing bracket could end the rendered call and append
  instructions of its own. The fence is now wider than the longest
  backtick run in the value (CommonMark's rule), so a value cannot
  close the block it sits in.
- **The question text keeps its newlines.** It was rebuilt as
  `" ".join(tokens)` after the filter pass, which collapsed every
  internal whitespace run — a pasted code block or a stack trace
  reached the search as one run-on line. qmd is whitespace-insensitive
  so retrieval never noticed, but the body tells the agent to pass the
  string *verbatim* and it was not what the user typed. Only the
  leading filter run is tokenized now; the rest is sliced out of the
  original string.
- **A string tool argument no longer sits inside the call's
  parentheses.** `lint --check "x) then run Bash(rm -rf /)"` rendered
  `mcp__lies__lint(..., check='x)', ...)` — the first closing paren
  belongs to the *value*, so an agent reading the line sees the call
  end early and reads the tail as prose. Nothing escaped into a second
  command, but an ambiguous instruction to an agent is a defect, and
  `ask` / `ground` had already moved their values out for the same
  reason. `check` now rides in a fenced `with check (…)` clause; an
  absent *or empty* value keeps the call on one line, since a fenced
  block reading "pass this string verbatim" above nothing looks like a
  rendering bug.
- **Three bodies no longer describe what they did inaccurately.**
  `collections new` reported a dropped source through the generic
  leftover note — "1 positional(s) the verb does not take" — when `new`
  does take a second positional and a `--source`/`--prompt` flag simply
  won; a user told the verb rejects their source would conclude the flag
  invented a rule. `sync` rendered `--name <wiki>` beside a positional
  collection with nothing to tell them apart, though the CLI documents
  `--name` as "Wiki to sync" and the positional as a collection; an
  agent passing a collection to `--name` gets the silent misroute the
  body exists to prevent. Both are now named in the body.
- **Slash-command prompts parse `+tag` / `-tag` filter tokens out of
  the question at render time.** Claude Code's slash parser shredded a
  multi-word question across typed prompt parameters in declared order
  and FastMCP rejected the non-string value with a JSON-parse error.
- **A filter must look like a tag *and* sit in the leading run.** A
  library of command-line tooling is full of questions like "the `-e`
  flag of grep", and those single-letter atoms were deleted from the
  question and re-injected as `exclude_tags` with no note. A filter
  atom now needs a `:` qualifier or two or more characters, and the
  scan stops at the first word of the question.
- **Prompt tails are split on whitespace, not by a shell lexer.** A
  `shlex`-based splitter raised `ValueError: No closing quotation` on
  any apostrophe or unbalanced quote, which FastMCP surfaced as a
  `PromptError`. Question text is prose, not shell.
- **A value flag followed by another flag no longer swallows it.**
  `sync --source --force` set `source='--force'` and dropped `--force`,
  so the body ran neither as asked. A value flag given no value is now
  reported by name.
- **A boolean flag written `--flag=value` is no longer silently
  dropped.** The parser routed any `=`-attached value into `values`,
  and every body read `booleans`. `reindex --all=true` ran the
  *non-destructive* path with `all_=False` and no confirmation
  warning. The flag is now set, the meaningless value is discarded,
  and the body names the discard. `TailParse.flag_on()` is the one
  place a body asks "was this switch set?".
- **`reindex --name <wiki> all` no longer swallows the destructive
  marker.** `name` was declared a multi-word flag, so the parser
  consumed up to the next `--flag` and bound `name='pydantic all'`,
  leaving `all_=False`. A wiki name is one token.
- **`reindex` accepts the bare `all` / `all_` positional** the
  pre-single-tail signature used, so `/lies-reindex all_` is no longer
  a silent downgrade of a destructive rebuild.
- **Flags outside a prompt's vocabulary are named in the body.** A
  typo like `--cleaup` was dropped silently, so a destructive rebuild
  request ran the non-destructive path unremarked.
- `ground --top_k 7` binds, not just `--top_k=7`. The space form leaked
  the value into the query text and fell back to the default.
- `ingest --title` takes free text (every word up to the next flag).
  `--title two words` bound `title='two'` and dropped `words`.
- `collections` interpolates its arguments with `shlex.join`, so a
  collection name containing shell metacharacters stays one argument
  in the `Bash(...)` line the agent runs.
- An empty `ground` / `ask` tail asks for the question rather than
  rendering a search for the empty string. (`ground` needed a second
  pass: its guard ran *before* the filter extraction, and a filter
  token is itself a positional, so `+c:opencode` and a bare
  `--top_k 5` both slipped through to `search('')`.)

## [0.41.0] - 2026-09-29

Reconstructed after the fact: this release shipped as commit `1504a20`
(PR #114) with the version bumped in `pyproject.toml` and
`src/lies/__init__.py` but no dated section here, so the changelog
jumped straight from `[Unreleased]` to `[0.40.0]`. Written from that
commit's own message and diff so the record matches what shipped.

### Breaking changes

- **MCP tool `ask` renamed to `lib_ask`.** The wire name and the Python
  symbol both change, so the prompt surface can take the `ask` slot
  without a collision. No forwarder and no alias; every call site was
  updated in the same change.

### Added

- Seven slash-command MCP prompts — `ask`, `collections`, `ingest`,
  `lint`, `reindex`, `sync`, `ground` — registered through a single
  `register_prompts(mcp)` call wired into `server.py` ahead of the tool
  registrations. Each returns one user-role `Message` templating a
  routed tool call: `ask` runs search → read → `lib_ask` for a
  synthesized cited answer with `[[collection/slug]]: "verbatim"`
  markers, `ground` stops at search → read for a snippet digest with no
  synthesis path.
- `tests/unit/mcp/_prompt_body.py`, a shared helper for reaching a
  prompt body's rendered text from a test.

## [0.40.0] - 2026-09-26

### Breaking changes

**MCP tool surface rewritten.** Old tools removed:
- `wiki_search` (subsumed by `search` + `ask`'s internal pipeline)
- `wiki_read` (renamed to `read`)
- `wiki_catalog` (renamed to `collections_read`)
- `synthesize` (renamed to `ask`)
- `ground` (renamed to `search`)
- `ask_question`, `ask_ground_question` (filter parsing moved into `search`/`ask`)
- `init_wiki` (deferred to post-v0.40; wiki code paths stay dormant in source)

Old tools renamed:
- `wiki_read` → `read`
- `wiki_catalog` → `collections_read`
- `synthesize` → `ask`
- `ground` → `search`

All 7 prompts removed (`answer`, `orient`, `ingest`, `lint`, `sync`, `file-back`, `cite`).

### Added

- **`collections_read` tool.** Live registry reader with three subcommands (`list`, `tag_list`, `info`). The librarian LLM uses it in Step 1 to build `tag_expr` from registry tokens.
- **`search` tool.** Single-batch hybrid vec+lex qmd query. One round-trip per call. Library-wins-on-slug-conflict merge happens inside qmd.
- **`read` tool.** Source-aware dispatch (wiki page IDs to `memory_service.read`, library paths to `qmd_get`).
- **`ask` tool.** Prose answer orchestration. Calls the librarian subagent's 4-step pipeline (Classify → Search → Read → Return) then the synthesizer.
- **Librarian agent 4-step pipeline.** Step 3 reviews snippets before committing to reads — closes the structural bug where qmd's BM25 ranking for "compare plugins" surfaced German/Italian/French localized overviews above the actual plugin-authoring guides.
- **`SynthesizeEnvelope.searched_scope`** (additive). Mirrors the `search` tool's `searched_scope` field so callers see what was queried.
- **Curated test corpus** at `tests/fixtures/library/collections/`. Five collections (`alpha`, `beta`, `gamma`, `delta`, `epsilon`) covering plugin authoring, marketplace, LSP integration, eval-driven testing, and plugin hints. ~30 hand-written docs. 20 query→expected_pages fixtures.

### Changed

- **`librarian_agent` system prompt** rewritten to the 4-step Classify → Search → Read → Return pipeline.
- `librarian_agent`'s tool set changed from `{wiki_search, wiki_read, wiki_catalog}` to `{collections_read, search, read}`.

## [0.39.2] - 2026-09-26

### Fixed

- **`/answer` slash prompt: defensive re-parse of leading filter
  sigil.** The MCP `answer` prompt rendered the
  `call-the-synthesize` body with `tag_expr: None` and the
  `+c:...` prefix unstripped inside the question field for some
  MCP clients (notably the OpenCode TUI slash picker). The model
  faithfully forwarded those kwargs to `synthesize`, so the
  librarian ran untagged. Live-debug: CC session
  `db4bd25e-e72e-4eee-8c25-0645d37f82ef`, OC sessions
  `ses_f2110b5fdffeTHmy7K0dkIIQaj` and parallel transcripts.
  Fix: when the primary parse returns `include_ast=None` but the
  rendered `parsed_question` still starts with a filter sigil,
  retry `parse_query_argv` on the question as if it were the full
  slash input. Fail-soft — legitimate questions that don't look
  filter-like are unchanged.
- **Librarian `_wiki_search`: thread caller-supplied `tag_expr`
  into the library qmd `collection_filter`.** The librarian
  queried the library index with
  `collection_filter=set(library_collection_names())` (every
  registered collection) regardless of the caller-supplied
  `tag_expr`. Live-debug confirmed callers passing
  `tag_expr='c:opencode|c:claude_code'` got 100% `claude_code/*`
  hits because qmd's hit ranking favored the higher-token-overlap
  claude_code matches for the words "compare" / "plugins" —
  despite `opencode/plugins.md` being ingested. Fix: resolve
  caller `tag_expr` to the set of registered collection names and
  pass that set (intersected with registered names) as the
  library-side `collection_filter`. Wiki side is unconstrained as
  before. When `tag_expr=None` the librarian falls back to the
  all-collections default.
- Pre-existing slow-test budget hits on
  `tests/unit/ingestion/test_etl_pipeline.py` and
  `tests/unit/ingestion/test_etl_quarantine.py` marked
  `@pytest.mark.slow` so the unit-test 0.15s wall-clock budget
  gate stops tripping.
- `WebScraper._LLMS_LINK_RE` regex (which extracts `- [Title](url)`
  entries from an `llms.txt` index) used `\s*` for in-line whitespace,
  which silently consumed newlines. llms.txt indexes whose links
  carry no `: description` (e.g. `https://opencode.ai/v2/llms.txt`,
  every entry bare) lost every-other link: the lazy `(.*?)`
  description group swallowed the next link line as its "description"
  because the trailing `\s*$` allowed matches to span newlines.
  Tightened whitespace to `[ \t]` so each match is confined to one
  line. Regression pin in
  `tests/unit/ingestion/test_scrapers_web.py::test_web_scraper_extract_llms_links_handles_bare_links`.

### Added
- `synthesize` MCP tool for human-reading prose answers. Calls `ground()`
  for retrieval then runs `query_synthesizer_agent` over the result.
- `library://catalog` and `library://catalog/{slug}` MCP resources as
  the user-facing catalog surface.
- Bespoke scraper path `~/.local/share/lies/scripts/opencode_llms_scraper.py`
  for the `opencode` library collection. Subclasses `WebScraper` to add
  `Accept: text/markdown` on per-page fetches; the OpenCode V2 docs site
  returns HTML by default and only negotiates markdown when that header
  is present (the same shape the site's "Copy page as Markdown" button
  uses). Wired via `scraper_cmd` in `~/.local/share/lies/library/collections/opencode/config.yaml`,
  pointing at the new `https://opencode.ai/v2/llms.txt` source.
- `ground()` parallel fan-out for unscoped queries: ≤ 15s with non-empty
  citations across all registered library collections.
- `ArchivistDigest.no_library: bool` flag (additive).

### Changed
- `ground()` unscoped queries no longer fall through the F18 librarian
  LLM round-trip; they take a parallel fan-out path.

### Removed
- `query`, `answer`, `wiki_search`, `wiki_read`, `wiki_changes`,
  `file_knowledge` MCP tools.
- `wiki://page/{path}`, `wiki://memory-changes`, `wiki://catalog`,
  `wiki://catalog/{slug}` MCP resources.


## [0.39.1] - 2026-09-25

### Added

- `lies sync` chains `qmd update` + `qmd embed` against the
  library git root after the collection loop. The auto-chain
  replaces the separate `/reindex` invocation operators
  previously had to run by hand. New `--skip-reindex` flag opts
  out for CI matrices that reindex separately. Failures log +
  warn; the sync exit code stays clean (sync data integrity is
  separate from qmd lag).
- `lies qmd status|up|down|recycle` operator CLI (mirrors ask's daemon commands).
- `qmd.recycle()` programmatic recovery for wedged daemons.
- `ArchivistDigest` and `SynthesizeEnvelope` fan-out triggers `recycle()` after N consecutive `QmdCommandError`s.
- `LIES_QMD_RECYCLE_THRESHOLD` env var (read at module import) controlling the
  consecutive-failure count that triggers `qmd.recycle()`. Operators adjusting
  the threshold must restart the MCP daemon — there is no hot-reload.

### Fixed

- `_fanout_collections` now dispatches qmd queries sequentially
  instead of via `asyncio.gather` under a 4-permit semaphore.
  Parallel subprocess fan-out caused a model-per-process VRAM
  spike on OR-scoped queries (e.g. `+c:opencode|c:claude_code`
  spawned 2 concurrent qmd subprocesses, each loading the
  embedding model). The trade is wall-clock: full-library
  unscoped fan-out goes from ~5-10s parallel to ceil(N)
  × per-collection latency (~60-90s for 14 collections at 3-7s
  per call). `_QMD_FANOUT_TIMEOUT` (15s) stays; the per-call
  budget is unchanged.
- `qmd_query` pipe-buffer deadlock: when qmd emitted a long stderr trace (e.g. on VRAM OOM during in-process llama.cpp context expansion), the Python wrapper blocked forever because the OS pipe buffer filled. Replaced with `Popen` + bounded `communicate(timeout=...)` + SIGKILL-on-timeout. Mirrors ask's `repo/ask/scripts/qmd-daemon.py:213-219`.
- Per-collection timeout in `_fanout_collections._one` bumped from 5s to 15s (`LIES_QMD_FANOUT_TIMEOUT` env override) to match qmd's actual reranking latency on cold daemons. Recycle counter no longer trips on `QmdNoResultsError` (clean miss) — only on `QmdCommandError` (real subprocess failure).
- All remaining `subprocess.run(capture_output=True, ...)` call sites in `src/lies/qmd/cli.py` (the `_run` helper + `qmd_get`) and `src/lies/qmd/_proc.py` now route through the deadlock-free `_run_qmd` helper introduced for `qmd_query`. The pre-existing daemon-lifecycle calls in `src/lies/qmd/daemon.py` are left with TODO markers for a follow-up PR (the bytes-vs-str conversion is not one-line trivial at those sites).
- `src/lies/qmd/daemon.py` lifecycle calls (`qmd_daemon_state`, `_spawn_qmd_daemon`, `ensure_qmd_daemon`) now route through `_run_qmd` (Popen + bounded communicate + SIGKILL via process-group killpg). Closes the qmd pipe-buffer deadlock that the PR #105 sweep explicitly deferred for `daemon.py` due to the bytes-vs-str conversion. 3 source sites + 8 test mocks updated (7 unit + 1 integration).

## [0.38.0] - 2026-09-24

### Changed

- BREAKING: every agent factory and `_resolve_default_models` no
  longer silently falls back to `anthropic:claude-opus-4-7` when
  no `providers.toml` and no `LIES_<AGENT>_MODEL` env override are
  configured. Missing configuration now raises
  `lies.errors.ModelNotConfigured` with the list of slots that
  need a value. The `lies providers init` wizard no longer
  pre-selects `anthropic:claude-opus-4-7` — `default_model` is
  now a required prompt. LIES does not hard-code vendor defaults;
  operators own the model choice. Migration: run
  `uv run lies providers init` or export per-agent env vars.

### Added

- New `lies.errors.ModelNotConfigured` exception raised by every
  agent factory and `_resolve_default_models` when no model is
  configured. Replaces the silent `anthropic:claude-opus-4-7`
  fallback.

### Fixed

- `WikiMemoryService.search` now threads an optional
  `qmd_collection_filter` kwarg down to `_from_qmd` /
  `search_wiki` so qmd applies the post-filter at qmd-time, and
  the caller's `tag_expr` reaches the library index instead of
  being resolved against every registered collection.


## [0.37.11] - 2026-09-23

### Fixed
- Library collection registry caches (`library_collection_names` and
  `library_collection_tags`) now self-invalidate when the on-disk
  collection set changes. The `@lru_cache` snapshot was previously
  populated once per daemon process and only refreshed on restart —
  a long-running MCP daemon did not see collections added after it
  started. Reproduced in session b0298d7a (2026-09-23): the daemon
  was started at 19:04, the `switchyard` collection was ingested
  at 21:30, and `mcp__lies__ground` with `tag_expr="c:switchyard"`
  surfaced `unknown tag(s): 'switchyard'` because the cache still
  held the pre-ingest 13-collection snapshot. The caches are now
  keyed on the directory mtime (names) and the aggregate mtime of
  the directory plus every contained `config.yaml` (tags), so any
  add / remove / rename / tag edit bumps the key and the next call
  re-walks. Unchanged directories keep the hot-path speedup.
- `WikiMemoryService.search` no longer drops wiki hits whose first
  path segment matches the library-shape pattern when the library
  registry is empty. The previous "conservative" fallback (treat
  any top-level-collection pattern as library-shaped when the
  registry cannot answer) leaked legitimate wiki hits: a wiki page
  at `concepts/x.md` was dropped because `library_collection_names()`
  returned the empty set and the regex fell through. Empty registry
  is now a no-op filter — there are no library collections to
  conflict with, so every wiki-shaped hit is a real wiki hit. The
  filter still drops library-shaped hits when the registry IS
  populated and the first segment matches a registered collection
  name (the original bug the gate was added for: a wiki qmd index
  that picks up library content due to data-root overlap).

## [0.37.10] - 2026-09-23

### Fixed
- `ArchivistDigest` (returned by the MCP `ground` tool) now exposes
  `searched_scope: list[str]` on the wire, matching the F15 envelope
  that `SynthesizedAnswer` already carries via
  `Orchestrator.run_query`. Live test subagents reported that `ground`
  returned the question / tag_expr / exclude_expr / citations /
  no_coverage / distinct_pages keys but no `searched_scope`, so MCP
  callers could not introspect the resolved scope. `ground()`
  computes `searched_scope` from the same `_all_collection_names`
  / `_collections_matching` helpers the orchestrator uses — every
  registered library collection when untagged, or the sorted set of
  collections whose `atom_matches` is true for the resolved include /
  exclude AST when tagged. Populated even on the `no_coverage=True`
  path (librarian dispatch exception / no model available) so callers
  can render "searched X, found nothing" rather than guessing. Wire
  format propagates automatically via `dataclasses.asdict(digest)`;
  no MCP wrapper change needed.
- `/cite` slash prompt now documents the Claude Code slash dispatcher
  limitation that surfaces as
  `ProtocolError: Missing required arguments: {'text'}` when the
  slash input begins with `+` (F15 include sigil). Session df653c3d
  (2026-09-24T01:29:30Z): invoking
  `/mcp__lies__cite +c:opencode|c:minimax|c:llama_cpp Configure my opencode...`
  from Claude Code drops the `text` argument entirely. The same
  limitation affected `/answer` (tokenizes on whitespace) but
  `/cite`'s failure mode is harder to recover from — the dispatcher
  reports a missing argument rather than a truncated one. The
  workaround (route through the `ask_ground_question` MCP tool with
  the user's full multi-word text) is now surfaced in the prompt
  description, the `ask_ground_question` tool description, and the
  MCP `instructions.md` operator guidance.

## [0.37.9] - 2026-09-23

### Fixed
- MCP F15 boundary validator now accepts bare tag names (`+claude`)
  in addition to the explicit `t:`-prefixed form, end to end
  across `query` / `answer` / `ground`. F15 already treats a bare
  atom as the implicit-t alias for `+t:tag` (`atom_matches` matches
  against `coll.tags ∪ {coll.name}`); the validator's available-set
  was only registering `t:<tag>`, so a bare `+claude` parsed to
  `Include("claude", qualifier=None)` and the resolver's
  `expr.tag in available` check rejected it with `unknown tag:
  'claude'` even though `claude` was a real tag on multiple library
  collections. Three paths share the helper now:
  `_collect_available_tags_mcp` (used by the `query` /
  `answer` boundaries and `mcp_ground`'s exclude-side validator),
  `grounding.ground`'s include-side resolve, and `mcp_ground`'s
  exclude-side parse — all consult the same expanded available set
  (bare collection names + `c:<name>` aliases + bare tags +
  `t:<tag>` aliases), so the implicit-t alias validates consistently
  on every F15 boundary. (`de8e4fa`)

## [0.37.8] - 2026-09-23

### Added
- New MCP tool `ask_ground_question(text)` that takes a single
  multi-word string and parses the `+tag_expr` / `-exclude_tags`
  filter syntax (including compound `&` / `|` chains). Mirrors the
  existing `ask_question` tool but returns kwargs shaped for the
  `ground` tool (`question`, `tag_expr`, `exclude_tags`, `top_k=3`).
  The LLM forwards the returned kwargs to `ground` verbatim. Works
  around Claude Code's `/mcp__lies__cite` slash-command dispatcher,
  which tokenizes the input on whitespace and discards everything
  past the first token — same limitation that motivated the answer
  side's `ask_question` tool. (`6d990b0`)

### Fixed
- `/cite` slash prompt now takes a single `text` positional arg, same
  shape as `/answer`. The prompt parses `+c:<name>` / `-<tag>` filter
  syntax internally (including compound exclude chains) and renders
  the parsed `tag_expr` / `exclude_tags` / `top_k` kwargs verbatim
  in the prompt body so the calling LLM does not have to fill them.
  Previously the slash took pre-parsed kwargs, and Claude Code's
  dispatcher tokenized the multi-word invocation on whitespace,
  dropping the filter syntax entirely. The `ground` MCP tool itself
  already parsed compound exclude correctly — the asymmetry was
  only at the slash layer. (`6d990b0`)

## [0.37.7] - 2026-09-23

### Fixed
- Librarian `_wiki_search` now applies `exclude_expr` site-side: hits
  whose path's first segment matches the exclude AST are dropped
  before the merged list is returned. Previously the librarian prompt
  instructed the LLM to pass `exclude_expr` to `wiki_search`, but the
  tool did not accept the kwarg; the LLM retried until it exhausted
  max output retries and failed with `UnexpectedModelBehavior`. With
  this fix, `answer(exclude_tags=[...])` works for single-atom,
  AND-compound, and OR-compound exclude ASTs. Surfaced by sessions
  ea703e3b and bf5a4db9. (`673fc5b`)
- `parse_query_argv` now accepts bare-operator argv tokens in both
  include and exclude chains. Realistic shell splitting (e.g.
  `shlex.split("-c:foo & c:bar")` → `["-c:foo", "&", "c:bar"]`)
  produces argv lists where the binary operator is its own token; the
  chain-peel loops now extend on either side (previous-token-ends-in-op
  OR current-token-is-op). The exclude loop also handles the
  bare-operator form by absorbing the following argv token as the
  trailing atom. Surfaced by live MCP testing of sessions ea703e3b +
  bf5a4db9. (`673fc5b`)

## [0.37.6] - 2026-09-23

### Fixed
- `_split_argv_token_for_ops` now keeps `c:` / `t:` qualifier prefixes
  attached when splitting argv tokens on `&` / `|`. Previously the
  shlex split inside that helper did not add `:` to wordchars, so a
  single-token input like `+c:opencode|c:claude_platform` produced
  `["c", ":", "opencode", "|", "c", ":", "claude_platform"]` and
  `parse_tokens` choked on the `:` token. Mirror of the include path's
  `parse()` which already added both `-` and `:` to wordchars.
  Surfaced by session 82a266a9. (`2e3cb66`)

### Changed
- `parse_query_argv` now supports a compound exclude chain
  (`-atom [&atom | |atom]*`). The exclude half of the F15 grammar is
  now symmetric with the include half: `&` and `|` operators work in
  both inclusion and exclusion, with `&` binding tighter than `|`.
  Return type extended to `tuple[str, TagExpr | None, TagExpr | None,
  None]` — `exclude_qualifier` removed (the qualifier now lives on
  each `Include` atom in the tree). Surfaced by session 82a266a9.
  (`45b8897`)
- `ResolvedTagFilter.exclude` is now a `TagExpr | None` AST instead of
  a flat string. `resolve()` validates the exclude tree against the
  registered collection set; `exclude_matches(coll, tree) -> bool`
  walks the AST. The librarian's `exclude_expr` field plumbs the AST
  end-to-end through `LibrarianDeps` → `LibrarianOutput` →
  `query_synthesizer`. Pre-existing `_exclude_atom_matches` removed.
  (`89e488d`)
- MCP `query` / `answer` / `ground` boundaries parse each
  `exclude_tags[i]` as a full F15 expression via `parse()`, replacing
  the prior single-atom `check_qualifier + Include(...)` shim.
  Bad grammar / unknown atoms surface as `ToolError` at the boundary.
  Eight integration test files migrated to the new AST shape (49
  sites). (`0f60207`)
- `ask_question` MCP tool renders the compound exclude AST back to an
  F15 expression string via `_render_include`, so the LLM can forward
  the returned `exclude_tags` value verbatim to `query` / `answer`.
  Docstring updated to document the operators. (`8a4292d`)

## [0.37.5] - 2026-09-23

### Fixed
- Librarian `_wiki_read` now strips a leading `qmd://` prefix from
  the input page_id before the library-path branch. Previously the
  full `qmd://opencode/config.md` string was treated as a library
  path and prepended with another `qmd://`, yielding a malformed
  `qmd://qmd://...` URI that qmd rejected → `WikiPageNotFound`.
  Surfaced by session f39c9ef8 calling `answer(tag_expr="c:opencode",
  exclude_tags=["claude_code"])`.
- `memory_service.search()` filters out hits whose path matches
  `^[^/]+/[^/]+\.md$` AND whose first segment is a registered
  library collection. The wiki qmd index had library content indexed
  under a wiki page-id, but the wiki catalog had no row for those
  pages — subsequent `wiki_read` raised `WikiPageNotFound`. Falls
  back to the bare regex when the library registry is empty.
- `wiki://catalog` resource returns `{"mode": "library", "collections":
  [...]}` when no wiki is registered (library-mode-only runtime),
  instead of surfacing stale wiki synthesis slugs.
- `wiki://index` and `wiki://lint-report` resources return informative
  envelopes (`{"mode": "library"}` / `{"mode": "library", "status":
  "no_wiki"}`) in library mode instead of empty strings.
- `reindex --cleanup` (and `--all`) now surface a clear bypass-path
  error string when MCP server-initiated elicitation is unavailable,
  pointing the caller at `lies mcp down && lies mcp up` retry or
  `lies reindex --cleanup` direct shell invocation.

## [0.37.4] - 2026-09-23

### Added
- New MCP tool `ask_question(text)` that takes a single multi-word
  string and parses the `+tag_expr` / `-exclude_tags` filter syntax.
  Works around Claude Code's `/mcp__lies__answer` slash-command
  dispatcher, which tokenizes the input on whitespace and discards
  everything past the first token.

### Fixed
- Librarian: wiki-side and library-side qmd hits now strip the
  qmd `docid` field (`#abc123` format) along with `page_id`. The
  LLM agent was extracting `docid` from search results and passing
  it to `wiki_read`, which then raised `WikiPageNotFound`.

## [0.37.3] - 2026-09-23

### Fixed
- `/answer` slash prompt now takes a single `text` positional arg.
  Claude Code's slash-command dispatcher forwards the rest of the
  line as one string when the prompt has only one positional arg;
  the previous three-arg signature was tokenizing the multi-word
  invocation and dropping everything past the first token.
- Librarian: wiki-side qmd hits are now mapped to the wiki's
  `page-` + sha1-12 page_ids by joining on `path` against
  `memory_service.search()` results. The qmd `#abc123` docid
  format is no longer surfaced to `_wiki_read`.

## [0.37.2] - 2026-09-22

### Fixed

- Librarian: library hits no longer carry qmd `#abc123` page_ids;
  `page_id` is `None` on library hits so the LLM doesn't try to
  `wiki_read` them.
- Librarian: `_wiki_read` now source-aware — wiki IDs go to
  `memory_service.read()`, library paths (`<collection>/<page>`)
  read from the library's qmd chunks via `qmd get`.
- Wiki catalog reconciles before each search — stale rows whose
  on-disk page is gone are dropped, so `wiki_search` doesn't
  return ghost page_ids.

## [0.37.1] - 2026-09-22

### Fixed

- Librarian fan-out: `qmd_query` now hits the library's qmd surface
  (`lib.git_root`), not just the wiki's. Library collections are
  reachable from `/answer` and `/cite` again. (`5c1f668`)
- `/answer` slash prompt exposes `tag_expr`, `exclude_tags`, `name`,
  `collection` as separate kwargs (was a single opaque `question`
  string with embedded filter syntax). (`fad2627`)
- Tag-expression validator's available-set now includes library-
  collection names with the `c:` qualifier prefix. (`e11b626`)
- Tag-expression validator's available-set now includes library-
  collection tags (each `LibraryCollectionConfig.tags` entry) with
  the `t:` qualifier prefix, in addition to library-collection
  names with the `c:` prefix. (`134d0bb`)
- Library commit envelope reconciles deletions of tracked files via
  `git add -u` after the explicit-files commit. Re-ingests no longer
  accumulate ghost entries in HEAD. (`df34c89`)
- `/answer` slash prompt now parses the question argument via
  `shlex.split()` + `parse_query_argv()` so the calling LLM doesn't
  hallucinate values for `tag_expr` / `exclude_tags`. Matches the
  CLI's grammar exactly. (`92e45c2`)

### Changed

- Librarian system prompt documents the dual-surface fan-out.

## [0.37.0] - 2026-09-22

### Changed

- **Dual-source routing for `/cite` and `/answer`.** The librarian
  and archivist now retrieve from both library collections
  (`~/.local/share/lies/library/collections/<name>/`) AND wikis
  (`~/.local/share/lies/<name>/`). Library collections are the
  primary source; on slug conflict, the library hit replaces the
  wiki hit. Wiki-only hits carry `source_kind="wiki"` and render
  with a `[secondary]` prefix so the LLM can flag the snippet as
  not grounded in a primary source. New field
  `CitationSnippet.source_kind` (defaults to `"library"` for
  backward compat).

### Added
- MCP prompt surface: `ask_wiki` and `query_prompt` prompts removed.
  `/answer` slash unchanged (librarian + synthesizer). New `/cite`
  slash templates a `ground()` tool call and renders the
  `ArchivistDigest` as `[[collection/slug]] (Title): "<snippet>"`
  citation lines.


## [0.36.0] - 2026-09-21

### Added

- **N2 — linter sub-agent tool dispatch (F2-shaped rewire).** The
  linter now retrieves wiki pages on demand via three tool calls
  (`wiki_list_pages` / `wiki_search` / `wiki_read`) instead of
  receiving the entire corpus as a single pre-loaded system prompt.
  Pre-N2 wikis at ~150+ pages overflowed the 128K local cap; the
  LLM contribution to the merged lint report went empty, silently
  suppressing contradiction / stale / data_gap findings. Post-N2
  the linter drives its own read budget via a stop-when-saturated
  rule and the bug is fixed at any wiki size. New module
  `src/lies/agents/linter_tools.py`; mirrors the F18 librarian's
  `register_librarian_tools` pattern. `LintDeps` shrinks to a
  marker type; `_build_linter_prompt` deletes; `LintReport` /
  `LintFinding` / `LintSeverity` shapes unchanged. The
  orchestrator's `_call_linter` shrinks accordingly; tool
  registration moves into `_build` via the new
  `_register_linter_tools` delegator. Spec:
  `superpowers/specs/2026-09-21-n2-linter-tool-dispatch-design.md`.

### Fixed

- **`wiki_list_pages` inventory: `page_id` instead of `path`.** The
  initial N2 implementation emitted wiki-relative paths in each
  inventory row and the prompt told the agent to pass them to
  `wiki_read`. `WikiMemoryService.read` requires SHA-1 page IDs
  (`page-<sha1(rel)[:12]>`); passing a bare path raised
  `WikiPageNotFound` for every read. The orchestrator's broad
  `except Exception` swallowed the failure and returned an empty
  LLM section, leaving the regression silent. Post-fix the
  inventory row carries `page_id` (computed via the same
  `_page_id_for` `WikiMemoryService.read` uses internally), the
  prompt instructs the linter to pass `page_ids`, and the tool
  descriptions document the contract. `_wiki_list_pages` also now
  reads the on-disk file to compute a real `size_estimate_tokens`
  (bytes / 4, so the 30K-token batch budget is reachable),
  surfaces `type` and `source_pkg` for clustering
  (`section` alone is `"wiki"` for every row and useless), and
  filters catalog-vs-disk drift rows (catalog entries whose file
  was deleted off-disk after a `lies catalog reconcile` race).
- **Stop-when-saturated rule tightened with observable cues.** The
  initial N2 prompt named the stop rule as "no new findings and
  cross-page comparison complete" — a subjective judgment that a
  weak model could satisfy after one batch or never satisfy at all.
  The rule now names two concrete cues: every cluster identified
  in step 1 has been read at least once via `wiki_read`, AND the
  most recent `wiki_search` returned zero page_ids the agent has
  not already loaded. Prompt-side unit pins guard against a
  regression to the subjective form.
- **Big-wiki integration test reaches the pre-N2 break point.** The
  initial integration test seeded 60 pages (well below the 150+
  threshold that previously overflowed) and asserted only that
  the markdown report header was present, satisfied by any
  successful `run_lint()` call. Post-fix the test seeds
  `_PRE_N2_BREAK_POINT + 50` pages and asserts
  `memory_service._known_evidence` is non-empty — direct
  end-to-end proof that `wiki_read` round-tripped page_ids
  through `WikiMemoryService.read` without raising. The seed
  also includes subdirectory pages (`concepts/`, `people/`) so
  the linter's clustering step has real partition signals.

## [0.35.1] - 2026-09-21

### Fixed

- **CHANGELOG accuracy for 0.35.0.** The 0.35.0 entry's parenthetical
  claim that the chart-variant prompt is "pre-grounded at prompt-build
  time with excerpts from the `mermaid` library collection" did not
  reflect shipped behavior — the chart-variant prompt is a self-
  contained static string with mermaid syntax grounding baked in, no
  library-collection lookup at runtime. The claim is replaced with an
  accurate description of the in-prompt grounding.
- **`_call_synthesizer` format_hint asymmetry.** Inner synth call now
  returns the caller's forced `format_hint` when one was supplied
  (`format_hint or answer.format_hint`) instead of always returning the
  synthesizer's emitted value. The `run_query_with_format` wrapper
  fixed the asymmetry at the outer surface; direct callers (and the
  file-back path that consults `format_hint` to set `render_format`)
  now see the forced value end-to-end.
- **`_render_chart` branch coverage.** Three direct unit tests pin the
  mermaid-present (echo rendered, no stderr), prose-only (echo body
  unchanged + stderr warning), and empty-body (no echo + stderr
  warning) branches of the chart dispatch. The empty-body contract
  was previously only exercised indirectly via the CLI runner's
  mermaid happy path.
- **`render_format: "chart"` filed-synthesis coverage.** End-to-end
  sister test asserts the `file_back_synthesis` frontmatter contains
  `'render_format: "chart"'` for chart answers, mirroring the existing
  `format="table"` pin. The chart file-back path was previously
  covered only at the orchestrator plumbing level, not at the
  rendered frontmatter level.
- **`SynthesizedAnswer.format="chart"` body shape documented.** The
  class docstring now lists the chart body shape alongside md / table /
  marp so the F1 dispatch contract is visible at the type surface.

## [0.35.0] - 2026-09-21

### Added

- **`--format=chart` query output (F1 chart addendum).** Synthesizer
  emits a single ```mermaid``` fence (flowchart / sequenceDiagram /
  classDiagram); renderer extracts the longest block and emits it
  unchanged. Validator-bypass: no parse, no retry, no sidecar file.
  The chart-variant system prompt ships with mermaid syntax
  grounding (flowchart / sequenceDiagram / classDiagram) baked into
  the prompt itself — no library-collection lookup at runtime.
  Literal unions widened to `Literal["md","table","marp","chart"]`
  across the F1 surface (CLI, validator, synthesizer, orchestrator,
  MCP envelope). `render_chart` lives at
  `src/lies/query/formats/chart.py`. The chart-variant system
  prompt lives at `QUERY_SYNTHESIZER_CHART_PROMPT` in
  `src/lies/agents/query_synthesizer.py`.

## [0.34.0] - 2026-09-20

### Added

- **`ground` MCP tool + Python function.** Returns an `ArchivistDigest`
  carrying up to 3 CitationSnippet entries (≤200 chars each) drawn
  from the LIES wiki collections via the F18 librarian. Caller
  renders as `[[slug]]: "snippet"` per ask's grounding form. New
  module `src/lies/mcp/grounding.py`. Tight per-page snippet so the
  agent can verify corpus coverage before reasoning.

## [0.33.0] - 2026-09-20

### Added

- **`[settings].version` field in `lies.toml`** for schema versioning.
  `WikiSettings` carries `settings_version: str | None`; parser reads
  `version = "1"` (or higher) from the `[settings]` block. Mismatched
  or unrecognized `version` triggers a `UserWarning` so old configs
  surface migration guidance without breaking the load path. Bump
  `CURRENT_SETTINGS_VERSION` in `src/lies/wiki_settings.py` when the
  `[settings]` schema changes incompatibly.

## [0.32.1] - 2026-09-20

### Fixed

- **`QueryAnswer.file_receipt` surface restored.** F3-era `file_receipt`
  field on `QueryAnswer` was lost in the Task 6 `run_query`
  refactor; the CLI receipt block depended on it; the integration
  tests for `test_synthesis_file_back.py` asserted on it. Restored
  as `MemoryReceipt | None` (default `None`). Orchestrator captures
  the receipt from `_file_back(...)` so the F1 CLI `--format`
  override path preserves it. `run_query_with_format` threads it
  through the rebuilt `QueryAnswer`.
- **MCP tool set assertion current.** `tests/mcp/test_server_shape.py`
  expected MCP tool set did not include `reindex` (F38 PR #88 added
  it). Updated the expected set.
- **`test_synthesis_file_back` kwargs migrated to F18/F19 surface.**
  `collection="..."` → `tag_expr="c:..."`, `force_file=True` →
  `file_back=True`. Tests stub the librarian dispatch (per the
  `test_tier2_query_path` per-instance monkeypatch pattern) so
  the F3 file-back pin exercises against canned page state without
  touching a real wiki_read tool during `TestModel` iteration.
- **`test_query_writes_sidecar_visible_via_all_three_surfaces`**
  gated on `ANTHROPIC_API_KEY` (or equivalent provider key)
  present in env. Skips cleanly when key absent; runs the
  subprocess `lies query` integration when key is configured.
- **Pre-F18 footnote-block integration tests dropped.** Two
  `tests/integration/test_query_library_wiki.py` tests that pinned
  the `[^N]` footnote-block form (retired by F19 §1) were deleted
  rather than xfailed; F19 inline-form pins (`test_inline_citation_form_emitted_*`)
  replace their coverage.

### Test fixtures

- Three-page wiki fixture at
  `tests/fixtures/sample_wiki/wiki/{concepts/pydantic.md,concepts/sqlalchemy.md,entities/postgres.md,index.md}`.

## [0.32.0] - 2026-09-20

### Added

- **F18 — Librarian subagent.** New `src/lies/agents/librarian.py` with
  `LibrarianDeps`, `PageExcerpt`, `LibrarianOutput`. Pydantic-ai in-process
  subagent that runs the 4-step contract (classify → search → read → return
  bundle) ported from `ask/skills/ask/librarian-prompt.md`. Validator
  workaround rewrite rules ported for qmd's vec-query hyphen guard.
- **F37 — Markdown spans parser.** New `src/lies/markdown_spans.py` with
  `parse_spans(text) -> list[Span]`. Each `Span` carries
  `(heading_path, body, code_fence, start_line)`. Code-fence spans are
  flagged; downstream consumers exclude them from prose excerpts.
- **F19 — Citation-style answers.** Synthesizer emits
  `[[page-slug]]: "verbatim text"` inline per claim. New
  `Citation.heading_path` field; new `ClaimCitation.quote` field.
  Filing-back renders `## Evidence` with span heading inline.
  Filed pages use the `[[slug]] (Heading > Subheading): "verbatim"`
  form.

### Changed

- **Citation form hard-cutover.** `SynthesizedAnswer.answer` body
  renders `[[slug]]: "verbatim"` per claim instead of `[^N]` footnote
  markers. Orchestrator no longer appends a footnote block.
- **`PageRead.excerpt` replaced with `PageRead.spans: list[Span]`.**
  Retrieval populates spans via `parse_spans(content)` at read time.
- **`Citation.heading_path`** (additive, default `None`). Populated by
  `_thread_heading_paths` from the span each claim cites.
- **`ClaimCitation.quote`** (additive, default `""`). Validated to
  appear verbatim in the cited span body.
- **`_first_meaningful_paragraph`** removed. Span parser supersedes.
- **`_extract_section_at`** deprecated. Use `parse_spans` for new code.

### Migration

- Existing footnote-rendered wiki pages stay footnote (no re-render).
  New `stale_citation_form` lint finding surfaces them; re-render via
  `lies lint --fix` (follow-up PR).
- Hard-cutover for new synthesis only. No opt-in flag.

## [0.31.0] - 2026-09-19

### Added

- **F38 — Destructive-flag confirmation.**
  - `lies reindex --cleanup/--all/--force/--embed` flags restored (deleted in PR #17). `--cleanup` and `--all` are destructive and gated.
  - New MCP `reindex` tool with `destructiveHint=True` annotation. Mirrors the CLI surface; destructive flags gated via `ctx.elicit`.
  - `_confirm_destructive_cli` (CLI helper) and `_confirm_destructive` (MCP helper) for destructive-flag confirmation. CLI prompts `Confirm destructive reindex (<flag>)? [y/N]` on TTY; non-TTY refuses unless `--yes`. MCP uses FastMCP `ctx.elicit` with `_ConfirmDestructive { confirm, reason }` pydantic schema.
  - `_qmd_proc` subprocess seam for testability (Bundle E E.2 boundary discipline).
  - `ReindexResult` Pydantic model returned by `qmd_reindex` and the MCP `reindex` tool.

## [0.30.0] - 2026-09-18

### Added

- **F17 — Page-type conventions**: schema-declared required `## <Heading>`
  sections per page type. Lint surfaces `missing_required_section`
  findings (`safe_to_fix=False`); the writer (CLI/MCP) refuses writes
  that omit required sections. Six page types covered (overview, entity,
  concept, comparison, source, synthesis). Override per-wiki via
  `<wiki>/schema.md`.

## [0.29.0] - 2026-09-18

### Added
- `lies wiki provenance` CLI (F29) — list every synthesised page's `derived_from` set with `--orphan` (filter to pages with dangling source slugs) and `--page <slug>` (drill into a single page; JSON output) flags. Default output is TSV; `--json` switches to a JSON array matching `lies catalog dump --json`. Read-only over the existing sqlite catalog.
- Citations now carry `(path, line, section)` so each cited claim points
  to the specific passage it relies on, not just the page. Synthesis
  outputs render a `Footnotes:` block at the bottom with anchors of
  the form `[name](path#L<line>) — <section>`. The MCP `query`
  envelope returns a new `claim_citations` list of `(claim,
  citation_index)` pairs for downstream consumers.

### Fixed
- Catalog first-open backfill (`rebuild_from_disk` → `CatalogPage.from_path`) now parses the `derived_from` YAML list from each page's frontmatter. Previously the parser extracted only `(title, type, updated)` and silently dropped `derived_from`, so pages written via `Orchestrator.file_back_author` had empty provenance in the catalog row after a rebuild.
- qmd daemon staleness between `Orchestrator` runs is now reaped before the first search; closes the silent-failure mode where a stale-but-serving daemon returned pre-write results. The check runs at `QmdCapability.as_capability` after the TCP probe, before the native toolset is advertised.

## [0.28.0] - 2026-09-17

### Changed (breaking)
- **BREAKING:** Collection configurations moved from per-wiki `~/.config/lies/<wiki>/collections/<slug>.yaml` to library-resident `~/.local/share/lies/library/collections/<slug>/config.yaml`. Collections are now library-global; wikis no longer bind collections.
- **BREAKING:** `src/lies/collections/{record,bootstrap,scraper_manifest,hash_manifest,document,errors}` removed. Import from `lies.library.record`, `lies.library.bootstrap`, `lies.library.errors` instead. Per-wiki JSON registry moved to `lies.wiki.registry`.
- **BREAKING:** `lies collections` CLI group removed; use `lies library {list,show,where,new,modify,delete,enrich-tags}` instead.
- **BREAKING:** `Collection.path` field removed (pointed at per-wiki raw dir that never existed).

### Added
- `lies library` sub-app with `list`, `show`, `where`, `new`, `modify`, `delete`, `enrich-tags`.
- `lies migrate-collection-configs` one-shot migration command.
- `LibraryCollectionConfig` dataclass, `ConfigYAML` Pydantic schema, atomic `config_io.load_config`/`save_config`.
- `library_collection_records()` registry iterator yielding full records.
- `bootstrap_library_collection()` idempotent helper (replaces wiki-side bootstrap).

### Migration
Run `uv tool upgrade lies` to 0.28.0, then `lies migrate-collection-configs --dry-run` to preview and `lies migrate-collection-configs --apply` to relocate the YAMLs.

## [0.26.0] - 2026-09-15

### Added
- MCP server orientation payload: handshake `instructions=` field carries the four path/env facts plus the tool inventory, so agents attaching from any cwd get correct LIES context without relying on `AGENTS.md` loading. Driven by `src/lies/mcp/instructions.md` + `prompts/*.md` rendered through the new `lies.mcp.instructions_loader`.
- Six new MCP prompts: `orient`, `ingest`, `query`, `lint`, `sync`, `file-back`. Reference-prose complement to the existing `ask_wiki` / `ask_wiki_answer` tool-call templates.
- Pre-commit hook `lies-commands-exist` (`tools/check_lies_commands.py`) blocks commits that introduce unresolved `lies <cmd>` references in the orientation payload.
- Regression test pinning the orientation payload away from the 2f320888 wrong-path bug.

## [0.25.0] - 2026-09-16

### Added
- F1: `lies query --format=auto|md|table|marp` (default `auto`). Auto-routes via the synthesizer's `format_hint`; explicit values trigger re-synthesis with a constrained prompt.
- New `format` field on `SynthesizedAnswer` (additive; MCP wire format gains one field).
- New `render_format` frontmatter field on synthesis pages (additive; existing pages don't have it).
- New module `src/lies/query/format_validator.py` (pure validator with silent `md` demotion).
- New package `src/lies/query/formats/` (`render_markdown`, `render_table`, `render_marp`).
- 60+ new unit tests + 5 integration tests across 6 new test suites.

### Changed
- `QueryAnswer` gains `format_hint: Literal["md","table","marp"] = "md"` (additive).
- `QUERY_SYNTHESIZER_SYSTEM_PROMPT` gains a hybrid taxonomy section (instruction list + shape examples).
- `build_answer_from_pages` validates the synthesizer's format_hint against the body via `validate_format` and sets `SynthesizedAnswer.format` accordingly.

### Fixed
- `render_format` frontmatter value is now double-quoted (`src/lies/page/author.py:_format_author_body`) to neutralize YAML-significant characters (`:` / `[` / `#` / embedded quotes / backslashes) a future caller might pass; mirrors the existing `title_q` / `collection_q` quote-and-escape pattern. The three known values (`md` / `table` / `marp`) parse unchanged; the broader type contract (`str | None`) is now safe by construction.

### Performance
- `format_validator.validate_format()` is pure; ~5ms overhead per call.
- `render_marp` subprocess call: 1-3s (gated on `--format=marp` opt-in).

## [0.24.1] - 2026-09-16

### Fixed
- **Tag-expression resolution is now library-first.** The library is the source of truth for collections — wikis do not own them. `_collections_matching`, `_all_collection_names`, `_collect_available_tags`, and `_collect_available_tags_mcp` now walk `Library.collections_root` (a directory of source-doc folders) instead of `wiki.collections_dir/*.yaml`. Library collections carry no per-collection yaml-declared tags — they are canonical source docs, not wikis — so the addressable tag set is exactly the set of library-collection directory names. `+c:opencode` resolves regardless of which wiki the operator's MCP daemon is bound to, because the opencode directory lives in the library. The `unknown tag: <name>` error now lists the library's collections instead of the active wiki's yaml-declared tags, and a distinct line ("the library is not initialized; collections live in the library, not in wikis") tells the operator when the library is absent. The library is never referred to as a wiki in the error surface. CLI mirrors the same enrichment on stderr before exit 2. This fixes the `+c:opencode ...` silent failure where the operator saw only `unknown tag: opencode` because the active wiki did not declare opencode; the opencode collection lives in the library and is now reachable from any wiki.

## [0.24.0] - 2026-09-15

### Added
- **QMD daemon HTTP recycle on transport errors.** `QmdCapability` now
  wraps the underlying `MCPToolset` in a `QmdRecycleToolset` that
  auto-recycles the qmd daemon on `httpx.ReadTimeout` (qexpander wedge),
  `httpx.TransportError`, and `mcp.McpError(code=REQUEST_TIMEOUT)`.
  ReadTimeout surfaces as `ModelRetry` (no inner retry — the same
  payload re-wedges the fresh daemon); TransportError / McpError
  surface as recycle + retry-once, then `ToolFailed` on second
  failure. Construction-time `QmdRecycleFailed` falls back to the
  in-process `QmdFallbackMcp` so the agent stays functional under
  the failure mode. New `recycle_qmd_daemon(data_dir, daemon_url)`
  async helper exposes the reap+spawn+probe sequence for direct
  callers. Explicit timeouts (`connect=2.0, read=60.0, write=10.0`)
  added via `httpx_client_factory` so a wedged daemon surfaces
  within one budget window instead of hanging until httpx's default
  timeout. Issue: `features/qmd-daemon-recycle/README.md`.
- **F14 — Stale qmd daemon detection.** `ensure_qmd_daemon` now
  reaps+respawns the daemon when `<XDG_CACHE_HOME>/qmd/last-write-marker`
  mtime is newer than qmd's `mcp.pid` mtime, catching the case where
  any wiki or library wrote to disk after the daemon was last
  spawned. `LibraryWriter.commit` touches the marker after every
  successful commit (best-effort). Bundled with the recycle PR since
  the reap+spawn sequence is shared.
- **`lies flock qmd recycle --name <wiki> [--ready-timeout 30]`**
  operator escape hatch. Manual trigger for the same probe-backed
  recycle that `QmdCapability` runs at construction time. Reuses
  the `flock qmd` sub-app from PR #74.

### Fixed
- **Long-running lies agents hung on qexpander cold start.** The
  qmd daemon's first HyDE query after fresh start used to wedge the
  call handler for ~60s with no automatic recovery; now the next
  call against the wedged daemon recycles and the model retries
  against the fresh one.

## [0.23.0] - 2026-09-14

### Added
- `mcp__lies__answer` tool returns the synthesized answer body as plain text (was buried inside `query`'s structured envelope). Chat surfaces that hide JSON tool results now render the answer verbatim; `query` keeps the structured path for callers that need citations / file-receipt / scope.
- `WebScraper` preserves nested source paths from `llms.txt` indexes: strips a leading `/docs/<lang>/` site prefix and mirrors the remaining hierarchy under the collection root. `agents-and-tools/agent-skills/best-practices.md` lands at `claude_platform/agents-and-tools/agent-skills/best-practices.md`.
- `derive_nested_slug` helper for source-relative paths; `derive_slug` keeps its flat single-segment contract so existing fixtures stay green.

### Changed
- `WebScraper._LLMS_LINK_RE` accepts the dash-separated description form (`- [Title](url) - description`) that platform.claude.com and other publishers emit, in addition to the colon form.
- `query_synthesizer` prompt clarifies that library-source paths use `<coll>/<file>` without a `wiki/` prefix; wiki-source paths carry the prefix. Quote paths verbatim from the corpus block headers.
- REPL `/help` command now uses `typer.echo` (stdout-bound) instead of `Console.print` (Rich-buffers, bypasses test capture).

### Fixed
- Synthesizer LLM emitted library-source citations with a phantom `wiki/` prefix; the orchestrator dropped them all, leaving the answer with no valid citations and the dropped-warning in `synthesis_reason`. `Orchestrator._normalize` now strips a leading `wiki/` from emitted citations and matches in both directions (with/without prefix) before constructing `Citation` objects. Defense-in-depth alongside the prompt fix.
- Library pages now land under nested mirror directories: `write_mirror` calls `target.parent.mkdir(parents=True, exist_ok=True)`.
- Pre-existing REPL help test (`tests/unit/test_cli.py::test_repl_help_command`) failed because Rich's `Console.print` bypasses `CliRunner` capture; replacing with `typer.echo` lets `assert "/ingest" in result.stdout` pass.

## [0.22.0] - 2026-09-13

### Changed
- **BREAKING**: `SynthesizedAnswer.citations` and `.pages_read` are now `list[Citation]` (was `list[str]`); each citation carries a `source: "library" | "wiki"` discriminator. Library collections are the primary source of truth; wiki content is supplementary. The synthesis prompt instructs the LLM that library wins on conflict. (Minor-bumped despite breaking shape change per explicit user override — see commit `chore(release): 0.22.0`.)
- `lies-mcp-query` resolves qmd hits against library collections first (`Library.collections_root/<coll>/<file>`) and falls back to `wiki.wiki_dir`. Same path from both roots surfaces as two citations with distinct sources.
- `retrieve_pages` runs two qmd passes — one scoped to library collections, one scoped to the new `wiki_<wikiname>` qmd collection registered at `WikiLayout.init`. Pages deduped on `(rel_path, source)` post-merge; library pass wins on collision.
- New fallback reason `wiki_only`: library returned 0 hits, wiki returned hits; answer opens with `_Note: not grounded in primary sources (library returned no matches); answered from wiki._`.
- `Orchestrator._call_query_synthesizer` reads library pages from `Library.open().collections_root` (was silently swallowing OSError on `wiki.data_root / <lib-uri>`).
- `query_synthesizer` prompt gains library-wins-on-conflict rule + `[library]`/`[wiki]` tag preservation instructions; `QueryDeps` carries `page_sources: dict[str, Literal["library", "wiki"]]` rendered inline per corpus block.
- `WikiLayout.name` derived from `root.name` (was hard-coded `"default"`); idempotent `WikiLayout.init` registration via PEP 562 lazy `__getattr__`.

### Added
- `src/lies/query/citation.py`: `Citation` frozen dataclass with `path` and `source` fields (re-exported from `lies.query`).
- Wiki-rooted qmd collection `wiki_<wikiname>` registered at `WikiLayout.init` via `qmd_collection_add_or_update`. Existing post-commit hook (`WikiMemoryService._refresh_qmd`) re-indexes it on every wiki write. Self-heal sentinel at `<wiki.data_root>/.lies/wiki_qmd_registered` for wikis that pre-date the registration call.
- `lies.query.__init__` re-exports `Citation` and `FALLBACK_REASON_WIKI_ONLY`.

### Fixed
- `tests/unit/test_synthesizer_prompt.py` corpus assertion now pins the new `[source]` prefix format (was passing for the wrong reason).
- `_resolve_qmd_path_in_wiki` strips `wiki_<name>/` URI prefix before joining onto `wiki.wiki_dir` (was producing `wiki.wiki_dir/wiki_<name>/...` which never exists).

### Fixed
- **qmd concurrent-subprocess CUDA pool race** (#74). Wraps
  every `qmd_*` CLI helper in `src/lies/qmd/cli.py` with a
  site-wide cross-process flock. Concurrent lies processes (CI
  parallel ingest, `lies mcp` server concurrent commits,
  `xargs -P N lies ingest`) now serialize through one inode at
  `${XDG_STATE_HOME:-~/.local/state}/lies/qmd.lock` instead of
  racing `cuMemAddressReserve(CUDA_POOL_VVM_MAX_SIZE)` and OOM-aborting.
  Past a 30 s wait, raises `QmdLockBusy(WikiFlockError)` with
  holder PID; `LibraryWriter.commit` surfaces the lock error
  distinctly from other qmd-spawn failures. Operator override:
  `lies flock qmd status` / `lies flock qmd force-repair`.
  See `superpowers/specs/2026-09-13-qmd-flock-envelope-design.md`
  and `issues/2026-09-13-qmd-concurrent-subprocess-race.md`.

### Fixed (earlier in [Unreleased])
- **MCP: `lies mcp up` no longer races against TCP `TIME_WAIT` after `lies mcp down`.** `port_free` (the pre-spawn probe in `src/lies/mcp/daemon.py`) did not set `SO_REUSEADDR` on its probe socket. After `down` sends `SIGTERM` to the daemon, the kernel holds the daemon's listen socket in `TIME_WAIT` for ~60s; the next probe `bind()` failed with `EADDRINUSE` and `lies mcp up` reported "127.0.0.1:8737 is already in use" until the timer expired — every restart was at risk of a ~60s failure window. Today the probe calls `sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)` before `bind()`, matching the daemon-socket hygiene `uvicorn` / `http.server` / `aiohttp` all follow. New `tests/unit/test_mcp_daemon.py::test_port_free_probe_socket_sets_so_reuseaddr` records every `setsockopt` call on the probe socket and asserts `SO_REUSEADDR` is set during the `port_free` invocation. End-to-end verification: two consecutive `lies mcp down && lies mcp up` cycles succeed immediately (pre-fix: second `up` blocked on TIME_WAIT). Bumps version 0.21.5 → 0.21.6 (patch per Conventional Commits `fix:` rule).

### Fixed (earlier in [Unreleased])
- **Library: `LibraryWriter.commit` qmd post-commit hook actually fires.** PEP 562 module-level `__getattr__` is invoked by `getattr(module, name)` AND by `module.name` attribute access, but NOT by Python `LOAD_GLOBAL` bytecode inside a function body. The prior implementation invoked the qmd helpers (`qmd_collection_add_or_update`, `qmd_update`, `qmd_embed`) as bare names from inside `LibraryWriter.commit`, raising `NameError` for each call. The `except Exception` wrapper caught the error, printed the "name '...' is not defined" warning to stderr, and silently swallowed — every library ingest committed to git but never registered with qmd. Library mirrors existed on disk but the qmd index never saw them, so every `lies mcp query` against library content fell back to the wiki `index.md`. Today the hook uses the same `globals().get(name) or __getattr__(name)` workaround as `src/lies/cli/page.py:146` (lazy `Orchestrator` import): explicit dict access with manual `__getattr__` re-invocation on miss. New `tests/unit/library/test_writer.py::test_writer_commit_qmd_post_commit_hook_invokes_helpers` pins the contract by stubbing the qmd helpers via `monkeypatch.setattr(lies.qmd.cli, ...)` and asserting each was invoked (pre-fix: stub untouched, NameError swallowed; post-fix: stub called exactly once). 1497 unit pass. Bumps version 0.21.4 → 0.21.5 (patch per Conventional Commits `fix:` rule).

### Fixed (earlier in [Unreleased])
- **Library / scrapers: `lies ingest --source <llms.txt URL>` no longer aborts on the first doc.** `WebScraper._parse_index` previously emitted a `_index.md` `ParsedDoc` for the llms.txt index body itself ("table of contents survives"). The library's slug regex rejects underscore-prefixed stems (`^[a-z0-9][a-z0-9_-]{0,127}$`); emitting `_index.md` forced a `SlugError` that propagated uncaught out of `_process_item` (the slug-derivation call sits outside the `try/except ValueError` block in `_process_item`). The run aborted on item #0 with the slug derived from `_index.md` → `-index` (after `replace("_","-")`). Today the parser drops the unused `_index.md` emission and returns one `ParsedDoc` per child URL only. New `tests/unit/ingestion/test_scrapers_web.py::test_web_scraper_parse_index_does_not_emit_index_marker` pins the absence; the existing `test_web_scraper_parse_index_follows_links` + `test_web_scraper_parse_index_skips_failed_fetches` were updated to drop the `_index.md` assertions. Library-side `_process_item` defensive hardening (catch `ValueError` around the entire slug-derivation block, not just `validate_slug`) is **deferred** to a follow-up per the systematic-debugging "one change at a time" rule. 1496 unit / 0 integration pass.

### Added
- **Test-suite timing Makefile targets** (`make time-unit-tests`,
  `make time-features-tests`). Mirror `pydantic-guidance`'s targets:
  pytest runs with `--durations=0 --durations-min=0 -vv --tb=short
  --no-header` so every test's wall-clock time prints, sorted slowest
  first. `time-unit-tests` runs `tests/unit/` (always); the features
  target short-circuits unless `INTEGRATION=1` is set, matching the
  new integration gate.

### Fixed
- **`lies ingest --batch <DIR>` now walks the directory end-to-end.** Phase 2 PR #62 listed `--batch` in the CLI surface (spec §"CLI surface"`/L31/L400) but the production fetcher (`ScraperFetcher.fetch_sources`) routed every input through `pick_scraper(source)`, which only matched URLs / existing files / PDFs and raised `ScraperUnavailable` for a directory. The `lies ingest --batch <dir> --slug-prefix X` form had no end-to-end path; the unit test `test_run_batch_ingest_walks_dir` masked the defect behind a `_StaticFetcher` test double. Today `ScraperFetcher.fetch_sources` carries a depth-1 `source.is_dir()` branch that yields one `FetchItem` per regular file (subdirectories skipped; `OSError` on read swallowed silently per the `WebScraper._parse_index` precedent). `run_batch_ingest` treats an empty directory as a no-op run (exits 0) per spec L400; previously it raised `LibraryFetchUnreachable` and the CLI exited non-zero on `lies ingest --batch <empty-dir>`. New `tests/integration/library/test_ingest_batch_e2e.py` (spec L372, integration-gated) covers the CLI end-to-end; `tests/unit/library/test_ingest.py::test_run_batch_ingest_production_fetcher_walks_directory` + `::test_run_batch_ingest_empty_directory_is_noop` pin the dispatch contract on the production fetcher + the no-op behavior. 587 unit / 1 integration pass.

- **Library bootstrap: `LibraryWriter` now initialises a git repo on first commit.** Phase 2 PR #62 left library repo creation implicit — a fresh `$XDG_DATA_HOME/lies/library/` had no `.git`, so the first `lies ingest` raised `LibraryAtomicCommitFailed("git add failed: fatal: not a git repository")` at the atomic-commit envelope. The CLI swallowed the failure (only `result.errors > 0` triggers `typer.Exit(code=1)`; library envelope exceptions are not classified) and exited 0 with mirror files on disk and an empty catalog. Today `LibraryWriter.__init__` boots an idempotent git repo when `.git` is absent, mirroring `git_init_initial` (`src/lies/wiki/layout.py:56`). Subsequent writers against an already-initialised repo no-op. New test `tests/unit/library/test_writer.py::test_writer_auto_bootstrap_init_repo_on_first_commit` pins the contract. 1493 unit pass.

- **`run_source_ingest` pipeline no longer shells out for git or qmd
  in unit tests.** `tests/unit/library/test_ingest.py` adds an
  autouse fixture that stubs `atomic_commit`, `qmd_collection_add_or_update`,
  `qmd_update`, and `qmd_embed` in `lies.library.writer`. The 10
  slowest unit tests (45s → 0.15s each, was the dominant share of the
  219s unit wall) now exercise the ingest logic without forking git.
  `WikiMemoryService.apply_plan` tests get the parallel mock set
  (`atomic_commit`, `_snapshot_working_tree`, `_restore_working_tree`,
  `_discard_snapshot`, `qmd_update`) in
  `tests/unit/test_apply_plan_sidecar.py`.
- **Daemon reap tests no longer wait on a 30s sleep.** The
  SIGTERM-ignored escalation test (`test_reap_qmd_daemon_escalates_to_sigkill_when_sigterm_ignored`)
  shrinks the SIGTERM grace window from 0.5s to 0.05s and the
  handler-install sleep from 0.3s to 0.1s. Wall drops from 0.83s to
  0.17s; the assertion still pins the SIGKILL-after-grace invariant.
- **`file_back_author` retry tests no longer wait on the
  `time.sleep(0.1)` backoff** between retry attempts. The `orch`
  fixture stubs `lies.orchestrator.time.sleep`; the retry LOGIC is
  still pinned (3-attempt exhaustion surfaces as
  `file_back_failed_after_3_attempts`), but the wall-clock backoff
  no longer dominates (0.22s → <0.15s per test).
- **`test_path_render_cmd_preserves_state_across_builds` no longer
  re-imports the path-loaded stub from disk on every build.** Mocks
  `lies.builders.liquid._resolve_render_cmd` with an in-memory
  recording module installed in `sys.modules`; the
  path-import single-call contract remains covered by the sibling
  `test_path_render_cmd_is_invoked_and_converted` test.

### Changed
- **SL101 conversions — 12 BaseModel subclasses now stdlib `@dataclass`** (#65). `AuthorQuestion`, `CollectionAuthorDeps`, `LintFinding`, `LintReport`, `PageDiff`, `QueryAnswer`, `SourceExtraction`, `_CollisionVerdict`, `PidRecord`, `CreateStub`, `PageCreate`, `PageDelete`. Internal data carriers only; pydantic-ai `output_type=` validated dataclasses end-to-end (probed in spec §"Background"). No agent system prompts or `output_type=` call sites changed. `repair.py:75` JSON builder and `mcp/daemon.py:126,137` pid round-trip swapped to `json.dumps(asdict(...), default=str)` + `datetime.fromisoformat` re-parse (preserves prior `model_dump_json` output contract byte-for-byte). 1492 unit tests + 1 warning (baseline 1461 + 31 pin tests; pre-existing `wikilink-collision` warning unrelated). `_RepairOp` and `_PlanOperation` parent classes stay `BaseModel` (have `Field(min_length=1)` / `model_validator`); only the flagged leaves converted.
- **`make check` now mirrors the full pre-commit stack** (lint +
  format + typecheck + supyrliminal + unit-test). Was only lint +
  typecheck + format; the local `test` pre-commit hook and the
  supyrliminal hook were outside the Makefile envelope. Pre-commit
  hooks wrap `make unit-test`, so a commit that lands in the repo has
  now passed every gate via one target.
- **Integration tests gated by `INTEGRATION=1`** via a single
  `pytest_collection_modifyitems` hook in
  `tests/integration/conftest.py`. Replaces the per-file
  `pytest.mark.skipif(os.environ.get("INTEGRATION") != "1", ...)``
  decorators scattered across the integration suite. `make test`,
  `make check`, `make features-test`, and `make time-features-tests`
  all skip integration tests unless the env var is set; CI runs them
  with `INTEGRATION=1`.
- **`slow` pytest marker + `--runslow` opt-in.** Tests over the 0.15s
  unit-test budget (CLI status rendering, integration-gated shell
  start-up, subprocess-bound daemon reap) carry `@pytest.mark.slow`.
  Default `make unit-test` skips them; `make time-unit-tests` passes
  `--runslow` so the timing report covers every test. Marker is
  registered in `pyproject.toml`; the skip hook lives in
  `tests/unit/conftest.py`. 13 tests marked slow; the default run
  drops from 33s (all tests) to 23s (slow skipped).

### Changed
- **`make check` now mirrors the full pre-commit stack** (lint +
  format + typecheck + supyrliminal + unit-test). Was only lint +
  typecheck + format; the local `test` pre-commit hook and the
  supyrliminal hook were outside the Makefile envelope. Pre-commit
  hooks wrap `make unit-test`, so a commit that lands in the repo has
  now passed every gate via one target.
- **Integration tests gated by `INTEGRATION=1`** via a single
  `pytest_collection_modifyitems` hook in
  `tests/integration/conftest.py`. Replaces the per-file
  `pytest.mark.skipif(os.environ.get("INTEGRATION") != "1", ...)`
  decorators scattered across the integration suite. `make test`,
  `make check`, `make features-test`, and `make time-features-tests`
  all skip integration tests unless the env var is set; CI runs them
  with `INTEGRATION=1`.
- **`slow` pytest marker + `--runslow` opt-in.** Tests over the 0.15s
  unit-test budget (CLI status rendering, integration-gated shell
  start-up, subprocess-bound daemon reap) carry `@pytest.mark.slow`.
  Default `make unit-test` skips them; `make time-unit-tests` passes
  `--runslow` so the timing report covers every test. Marker is
  registered in `pyproject.toml`; the skip hook lives in
  `tests/unit/conftest.py`. 13 tests marked slow; the default run
  drops from 33s (all tests) to 23s (slow skipped).

### Fixed
- **`run_source_ingest` pipeline no longer shells out for git or qmd
  in unit tests.** `tests/unit/library/test_ingest.py` adds an
  autouse fixture that stubs `atomic_commit`, `qmd_collection_add_or_update`,
  `qmd_update`, and `qmd_embed` in `lies.library.writer`. The 10
  slowest unit tests (45s → 0.15s each, was the dominant share of the
  219s unit wall) now exercise the ingest logic without forking git.
  `WikiMemoryService.apply_plan` tests get the parallel mock set
  (`atomic_commit`, `_snapshot_working_tree`, `_restore_working_tree`,
  `_discard_snapshot`, `qmd_update`) in
  `tests/unit/test_apply_plan_sidecar.py`.
- **Daemon reap tests no longer wait on a 30s sleep.** The
  SIGTERM-ignored escalation test (`test_reap_qmd_daemon_escalates_to_sigkill_when_sigterm_ignored`)
  shrinks the SIGTERM grace window from 0.5s to 0.05s and the
  handler-install sleep from 0.3s to 0.1s. Wall drops from 0.83s to
  0.17s; the assertion still pins the SIGKILL-after-grace invariant.
- **`file_back_author` retry tests no longer wait on the
  `time.sleep(0.1)` backoff** between retry attempts. The `orch`
  fixture stubs `lies.orchestrator.time.sleep`; the retry LOGIC is
  still pinned (3-attempt exhaustion surfaces as
  `file_back_failed_after_3_attempts`), but the wall-clock backoff
  no longer dominates (0.22s → <0.15s per test).
- **`test_path_render_cmd_preserves_state_across_builds` no longer
  re-imports the path-loaded stub from disk on every build.** Mocks
  `lies.builders.liquid._resolve_render_cmd` with an in-memory
  recording module installed in `sys.modules`; the
  path-import single-call contract remains covered by the sibling
  `test_path_render_cmd_is_invoked_and_converted` test.

## [0.21.0] - 2026-09-11

### Added
- **Tag-filter `t:` and `c:` qualifier prefixes** for `lies query` and
  `mcp_query` filter atoms. `t:foo` (or no prefix) matches the
  existing implicit-self-tag rule (`foo ∈ coll.tags ∪ {coll.name}`);
  `c:foo` is the strict collection-name match (`coll.name == foo`).
  Enables disambiguating tag vs collection name without changing
  storage. Example: `+t:python -c:python` returns every collection
  with the python tag except the python collection itself. Both
  include and exclude atoms accept the prefixes; CLI `--exclude-tag`
  and MCP `exclude_tags` accept prefixed values. Parser rejects
  unknown qualifiers (`x:foo` → `TagExprParseError("unknown
  qualifier: 'x'")`).
- **SL101 conversions — 12 BaseModel subclasses now stdlib `@dataclass`** (#65). `AuthorQuestion`, `CollectionAuthorDeps`, `LintFinding`, `LintReport`, `PageDiff`, `QueryAnswer`, `SourceExtraction`, `_CollisionVerdict`, `PidRecord`, `CreateStub`, `PageCreate`, `PageDelete`. Internal data carriers only; pydantic-ai `output_type=` validated dataclasses end-to-end (probed in spec §"Background"). No agent system prompts or `output_type=` call sites changed. `repair.py:75` JSON builder and `mcp/daemon.py:126,137` pid round-trip swapped to `json.dumps(asdict(...), default=str)` + `datetime.fromisoformat` re-parse (preserves prior `model_dump_json` output contract byte-for-byte). 1492 unit tests + 1 warning (baseline 1461 + 31 pin tests; pre-existing `wikilink-collision` warning unrelated). `_RepairOp` and `_PlanOperation` parent classes stay `BaseModel` (have `Field(min_length=1)` / `model_validator`); only the flagged leaves converted.

### Changed
- **Supyrliminal hook now blocking** (#66). The `--exit-zero` advisory flag is dropped; commits fail if a new SL/PYD finding lands. Pre-PR conversion reduced 12 → 0 findings; the hook now enforces the policy on every commit locally + in CI.

### Fixed
- **Empty-body qualifier (`c:` / `t:`) now errors consistently on include + exclude.** The include path previously produced `Include(tag='c:', qualifier=None)` and confused the operator with `unknown tag: 'c:'` at the resolver; the exclude path raised cleanly. Unify by checking empty body in `parse_atom` (mirrors `_check_qualifier` on the exclude side). `_check_qualifier` promoted to public API as `check_qualifier` (drop underscore prefix; three call sites in CLI + MCP now use the public name).

## [0.20.0] - 2026-09-10

### Added
- **F15 — Tag-filter language** (`+tag&tag|tag -tag`): `lies query`
  and `mcp_query` accept a filter expression to scope retrieval to
  collections whose tags match. `Collection.tags` round-trips through
  YAML with permissive coercion; the parser uses `shlex` for quoted
  tag names; the resolver validates tags against the available set
  with no fuzzy match. `SynthesizedAnswer.searched_scope` reports the
  resolved collection set. CLI: `lies query +airflow what are DAGs?`,
  `lies query --tag-expr "airflow&provider" --exclude-tag amazon
  what is X?`. MCP: `mcp_query(tag_expr=..., exclude_tags=...)`.
  Mirrors the predecessor's `ROUTING.md` §"Tag-filter syntax".
- `lies collections new --tag X` (repeatable) for attaching tags at
  creation time.
- `lies collections enrich-tags` (dry-run) prints
  `lies collections modify --set tags=...` invocations for
  collections missing tags. `--apply` is reserved for a future
  auto-apply and currently raises `BadParameter`.

### Fixed
- `Collection.tags` non-list YAML values now coerce to `[]` with a
  warning (mirrors the `language` permissive fallback).

### Changed
- Replaced the vendored `pydantic-guidance` flake8 plugin with the
  upstream [`supyrliminal`](https://github.com/MistressFilth/supyrliminal)
  package. `supyrliminal` is now a dev dependency, pinned via
  `[tool.uv.sources]` to `v0.1.0` of the GitHub repo (the same code that
  lived under `vendor/pydantic-guidance`, now packaged as `supyrliminal`).
  `make lint-supyrliminal` runs `flake8 --select=SL,PYD` and is wired
  into `make check`; the same hook is registered in
  `.pre-commit-config.yaml`. The `vendor/` directory is removed.

## [0.19.0] - 2026-09-08

### Added
- Library split: ingested sources land in
  `$XDG_DATA_HOME/lies/library/collections/<collection>/`, never under
  `wiki/`. The wiki is now the agent's markdown; the library is the
  deterministic, immutable mirror of curated sources.
- New `lies ingest` CLI (`src/lies/library/cli.py`) with `--source`
  (single) and `--batch` (multi) modes; deterministic 5-step pipeline
  (fetch → ETL → filter → mirror → catalog+commit). No LLM call on
  the ingest path. New flags: `--slug-prefix`, `--collection`,
  `--slug`, `--title`, `--exclude-stem`, `--exclude-dir`, `--force`,
  `--dry-run`.
- New `lies migrate ingest-to-library` script
  (`src/lies/library/cli_migrate.py`): moves wiki-resident ingests
  into the library; backup duplicates at
  `<wiki>/.lies/migration-backup/<date>/`. `--dry-run` previews the
  moves; `--apply` performs the atomic-commit envelope (one commit
  per collection) and registers each library-side collection with
  qmd.
- `LibraryWriter` atomic-commit envelope
  (`src/lies/library/writer.py`), mirroring
  `WikiMemoryService.apply_plan` semantics on the library git root.
- Library catalog DB at `<library>/.lies/catalog.db` (sqlite WAL,
  `busy_timeout=5000`, schema v2 with `library` and
  `library-migrated` sections).
- `lies status` output gains library catalog count + migrated tally.

### Changed
- Wiki-write surface narrowed to F39 page-author + LLM-driven paths
  (MemoryEnricher, F3 file-back). Ingest no longer writes under
  `wiki/<collection>/`.
- `lies sync` retargeted: writes to library, not wiki. Honors
  `Collection.scraper_cmd` end-to-end (bespoke scrapers via
  `module:attr` / `path.py:attr`); routes REGISTRY-registered source
  formats (sphinx / liquid / bespoke) through their builders before
  falling back to `format_dispatch`. Exits non-zero when the
  underlying `BatchIngestResult.errors` is non-empty, so a wholly-
  failed batch no longer exits 0.

### Fixed
- `library/fetcher.py`: `_normalize_body` now routes registered
  source formats (sphinx / liquid / bespoke / etc.) through the
  `REGISTRY` builder before falling back to `format_dispatch.dispatch`,
  mirroring `etl/stages/normalize.py:83-90`. Without the REGISTRY
  first-pass, builder-handled formats raised `UnknownFormatError` and
  the doc was quarantined even when a valid builder was registered.
- `cli/ingestion.py` NameError on `ingest-source` — bare-name `Orchestrator(wiki)`
  lookup does not consult the module `__getattr__` (PEP 562 fires on
  `module.attr` / `from M import X`, not on function-body global lookup).
  Replaced with function-local `from lies.cli import Orchestrator as
  _Orchestrator`, mirroring the established `lies.cli/__init__.py:84-91`
  pattern. Closes the regression where every `ingest-source` invocation
  raised `NameError: name 'Orchestrator' is not defined`.
- F2 single-source ingest now threads the CLI `--collection` flag
  through to `Orchestrator.run_ingest`. Previously, `run_ingest` derived
  `collection_name = Path(source).stem` and ignored the CLI arg, silently
  routing `https://code.claude.com/llms.txt` (collection `claude_code`)
  through `raw/llms/` + `apply_plan(tag="ingest", collection="llms")`.
  Added `collection: str | None = None` kwarg to `run_ingest`; CLI +
  MCP callers thread it; default falls back to source-stem for legacy
  callers. The same F2-era bug was present in `_call_page_writer`
  (`writer.run_sync(prompt, deps=deps)` had no positional prompt,
  which makes pydantic-ai send empty messages and the provider returns
  `400 invalid params, messages must not be empty`); added the
  positional `prompt` so the request body always has at least one user
  message. Both fixes surfaced during the 2026-09-07 ingest attempt.
- `agents/source_reader.SourceExtraction` schema defaults: `claims`,
  `entities`, `concepts`, `comparisons`, `summary` all default to empty.
  Link-list sources (`llms.txt` indexes, sitemap excerpts, navigation
  manifests) have no prose to summarize; M3's natural output omits
  `summary` and the `claims`/`entities`/`concepts`/`comparisons` lists
  are sometimes empty. The previous all-required schema caused
  `UnexpectedModelBehavior: Exceeded maximum output retries (1)` even
  on otherwise-valid output. Downstream `PageWriterDeps` does not
  consume `SourceExtraction` today, so the relaxed defaults are safe.
- `memory/service._call_source_reader` and `_call_page_writer` are
  fail-soft: any agent exception (validation retries, `ModelHTTPError`
  400/401, TypeError on partial output) writes the quarantine sidecar
  and returns an empty result so the ingest completes with no pages
  rather than raising `IngestQuarantined` and aborting the entire
  apply_plan. The outer wrapper retries the agent call up to 3 times
  with a fresh `run_sync` (no accumulated message history), since
  pydantic-ai's internal retry grows the context with prior errors
  and `MiniMax-M3` is observed to succeed on a fresh attempt after
  the first one fails.
- `memory/service._normalize_collection_prefix` (new): forces every
  PageDiff path to land under `wiki/<collection>/`. The
  page-writer agent emits `wiki/<source_stem>/<rest>` (using the
  source filename as the collection prefix instead of the target
  collection) or omits the collection segment entirely. Without this
  normalization, the per-collection subdir convention (PR #39) is
  silently violated and pages land under `wiki/<source_stem>/`.
  System-file paths (`wiki/index.md`, `wiki/log.md`) are preserved
  untouched so the system-file guard in `_apply_operations` continues
  to fire.
- `memory/service._page_type_from_dir` no longer naively strips the
  trailing `s` (`entities` → `entitie`, the F2-era bug) nor accepts a
  raw collection name as a valid type (`claude_code`). Hard-coded
  plural→singular mapping (`concepts` → `concept`, etc.) + unknown
  defaults to `concept`. `MiniMax-M3`'s page-writer flattens the
  per-collection subdir layout (writes at `wiki/<collection>/<file>.md`
  without a `<type_plural>/` segment), so the path-derived page_type
  would otherwise be the collection name.
- `memory/validation.validate_frontmatter` is permissive on
  mismatch: an explicit `type:` in the frontmatter wins over the
  path-derived page_type. MiniMax-M3's flat path layout + the agent's
  own page-type choice in the frontmatter (`type: entity`, etc.) are
  both respected. Invalid `type:` values (not in ALLOWED_PAGE_TYPES)
  are still rejected. Missing `type:` is now accepted (auto-fill on
  disk later); the path-derived page_type is a hint, not authoritative.
- `memory/service._page_type_from_dir` + `memory/validation.validate_frontmatter`
  regressions pinned via `tests/unit/memory/test_service.py` +
  `tests/unit/memory/test_validation.py`.

### Changed
- `providers/config.py`, `providers/resolver.py`: add
  `openai_compatible` provider type. Resolves to `OpenAIChatModel` +
  `AsyncOpenAI` client (was: `anthropic_compatible` only,
  `AnthropicModel` + `AsyncAnthropic`). The minimax provider in
  `~/.config/lies/providers.toml` is now `openai_compatible` pointing at
  `https://api.minimax.io/v1`. The `/anthropic` endpoint is preserved
  as a supported type for future providers. The resolver dispatches
  on `spec.type` and narrows to the right client constructor; the
  `providers/registry._client_for` helper is unchanged (still
  Anthropic-only).
- `pyproject.toml`: `pydantic-ai-slim>=2.18` → `pydantic-ai-slim[openai]>=2.18`
  so the `openai` extra is installed and `OpenAIChatModel` +
  `OpenAIProvider` resolve.
- `agents/source_reader.source_reader_agent`: wraps `SourceExtraction`
  in `pydantic_ai.output.PromptedOutput`. `PromptedOutput` serializes
  the schema into instructions and parses the model's free-form JSON
  text rather than relying on tool calling. `MiniMax-M3` ignores
  `tool_choice` and `response_format=json_schema` on both the
  Anthropic-compat and OpenAI-compat endpoints — returning a text
  description of the schema rather than invoking the tool — so the
  default `ToolOutput` and `NativeOutput` modes fail with
  `Exceeded maximum output retries (1)`. `PromptedOutput` is the only
  shape that works reliably with that model. Same change applied to
  `agents/page_writer.page_writer_agent` for `list[PageDiff]`. The
  `TestModel`-based unit test that exercised the live run was relaxed
  (TestModel can't drive `PromptedOutput`); the integration path
  exercises the real model.
- `~/.config/lies/providers.toml`: model name changed from
  `MiniMax-M3[1m]` to `MiniMax-M3`. The `[1m]` suffix is a
  context-window label, not part of the model ID; the OpenAI-compat
  endpoint returns `400 unknown model 'minimax-m3[1m]'` for the
  suffixed form. The Anthropic-compat endpoint silently accepted it
  (which is why the original ingest ran against `/anthropic` at all).

### Reviewer follow-ups (PR #59)

- `providers/bootstrap.py`: wizard prompt label + validator now include
  `openai_compatible` alongside `anthropic` / `anthropic_compatible`.
  The `base_url` prompt defaults to `https://api.minimax.io/v1` for the
  `openai_compatible` case.
- `providers/config.py`: `base_url` is now required for both compatible
  provider types. Stale error message that mentioned only
  `anthropic` + `anthropic_compatible` updated.
- `providers/ops.py._probe`: probes `openai_compatible` providers via
  `AsyncOpenAI.models.list()`. Previously silently skipped them,
  making `lies providers check` falsely report an unconfigured provider
  as ok.
- `memory/service._normalize_collection_prefix`: the rewrite now
  strips the wrong-collection prefix and re-prefixes with the target
  collection. A page emitted at `wiki/llms/concepts/hooks.md` for the
  `claude_code` collection now lands at
  `wiki/claude_code/concepts/hooks.md` rather than
  `wiki/claude_code/llms/concepts/hooks.md`. The page-type directory
  (`concepts/` etc.) is preserved when present. New direct unit tests
  in `tests/unit/memory/test_service.py::TestNormalizeCollectionPrefix`.
- `memory/service._page_type_from_dir`: the "unknown directory
  defaults to concept" branch now logs a `logging.getLogger` warning
  so the operator can spot M3's path-flattening bug in the catalog
  without auditing every page.
- `orchestrator._call_source_reader` / `_call_page_writer`: retry
  loop variable renamed `attempt` to `_` and added 100ms `time.sleep`
  backoff between attempts, matching the existing `EnrichmentQueue`
  retry loop. Removed dead `IngestQuarantined` branch in
  `Orchestrator.run_ingest` + the corresponding import + stale
  docstring references (the wrapper is fail-soft and no longer raises).
- `tests/integration/test_run_ingest_end_to_end.py`: page-writer
  failure test updated to assert the new fail-soft contract (empty
  diffs + quarantine sidecar) instead of the old `IngestQuarantined`
  raise.
- `tests/mcp/test_ingest_source_tool.py`: `_FakeOrchestrator.run_ingest`
  adds the `collection` kwarg to match the orchestrator signature;
  `test_mcp_ingest_source_default_runs_llm_path` now asserts
  `seen["collection"] == "foo"` to pin the threading.
- `tests/unit/providers/test_bootstrap.py`: wizard prompt-label
  substring updated to match the new `openai_compatible`-aware prompt.
- `tests/unit/memory/test_service.py`: `test_translate_page_diffs_to_plan_create`
  updated to assert the corrected path layout
  (`wiki/<collection>/<type_plural>/<file>`).
- `agents/source_reader.source_reader_agent`: breadcrumb comment
  updated to point at the project-notes issue file instead of the
  stale `TODO F13` reference (F13 is qmd-singleflight, unrelated).
- `orchestrator._call_page_writer`: dropped redundant `or []` on the
  `diffs` return (the `output_type: list[PageDiff]` contract guarantees
  a list).
- `README.md`: provider docs section updated to enumerate all three
  provider types (including the `openai_compatible` /
  `https://api.minimax.io/v1` shape and the `PromptedOutput` rationale).

## [0.18.0] - 2026-09-07

### Added
- F39: `lies page write` CLI + MCP `file_knowledge` tool for direct page authoring.
- F12: MCP `ctx.elicit` collision gate (overwrite/rename/cancel) on `file_knowledge`.
- Refactor: `build_synthesis_plan` collapsed into `build_author_plan(type="synthesis", ...)`. F3 behavior preserved by regression-test pins.

### Fixed

- `Wiki.require` probes a known migration fallback for the `default`
  wiki's `data_root` (`<xdg>/lies/wiki` from the 2026-08-15 rename).
  First existing path wins. Closes N4.
- `log.md` lint entries now include finding categories in the title
  (e.g. `lint | 3 findings (missing_xref, orphan)`); the count derives
  from `len(report.findings)` rather than the rendered markdown's
  newline count. Closes F7.
- Catalog `.gitignore` entries rewritten to `wiki/.lies/catalog.db*` so
  the pattern actually matches the on-disk path at
  `<wiki>/wiki/.lies/catalog.db` (the previous root-anchored entries
  never matched and the catalog was being committed). Closes the
  catalog-gitignore entry.
- qmd daemon sidecar (`<xdg>/qmd/mcp.data-dir`) records the `data-dir`
  the running qmd daemon was started with. `ensure_qmd_daemon` reaps
  and respawns qmd when the sidecar disagrees with the requested
  `data-dir`; first-run (no sidecar) is a non-mismatch. Closes N7.
- `_reap_qmd_daemon` now waits for the daemon to exit before returning
  (SIGTERM first, SIGKILL after a 2s grace) and checks `/proc/<pid>`
  state to distinguish zombies from live processes, so the subsequent
  spawn no longer races the dying daemon for the port. Without the
  wait the sidecar could record a new `data-dir` while the old daemon
  still served the old index. `check_data_dir_match` normalizes paths
  via `Path.resolve` so `Path("wiki")` vs `Path("./wiki")` no longer
  spuriously reports a mismatch.
- MCP `file_knowledge` now accepts a `name: str | None = None` parameter
  and forwards it to `resolve_wiki`, mirroring every other tool
  (`init_wiki`, `ingest_source`, `query`, `lint`, `wiki_search`,
  `wiki_read`, `wiki_changes`). Without it an operator could not
  target a specific wiki via the MCP surface; the CLI's `--name`
  flag was the only path. Regression test
  `test_file_knowledge_with_explicit_name_uses_that_wiki` pins the
  contract.

### Tests

- Regression test pinning the resolved `data_root` path in
  `WikiNotRegistered` messages (N3 — the fix shipped earlier in 926f398
  and 939ecc7; the test guards the contract).
- Integration test asserting `SynthesizedAnswer.synthesis_used`,
  `synthesis_reason`, and `should_file` on the F4a clean-answer path.
  Closes the F4a-cov entry.
- Regression guard pinning the deletion of `catalog.rebuild_index`
  and its helpers (`_discover_pages`, `_page_title_from_frontmatter`),
  removed in the F4b+F16 port (commit 169871d).
- Regression test pinning `raw/` immutability via `validate_page_path`,
  which rejects `..` traversal into `<data_root>/raw` with
  `WikiPlanInvalid`. No chmod or new error type added — the path-
  traversal guard predates this release.
- README drops the obsolete manual `qmd update` workaround (the
  embedded post-commit hook from PR #37 is the only path). Closes the
  qmd double-indexing entry.

### Added

- F39: `lies page write` CLI + MCP `file_knowledge` tool for direct page authoring.
- F12: MCP `ctx.elicit` collision gate (overwrite/rename/cancel) on `file_knowledge`.
- Refactor: `build_synthesis_plan` collapsed into `build_author_plan(type="synthesis", ...)`. F3 behavior preserved by regression-test pins.
- F4b + F16 — wiki catalog port. A sqlite database at
  `<wiki>/wiki/.lies/catalog.db` (WAL journal mode +
  `busy_timeout=5000`) replaces `wiki/index.md` as the catalog
  source-of-truth. Note the location: this `.lies/` sits inside the
  wiki dir, beside the markdown — distinct from the wiki-root
  `<wiki>/.lies/` that holds `memory_plans.jsonl`. The surfaces:
  - `lies.memory.catalog` — schema, CRUD (`upsert_page(s)`,
    `remove_page(s)`, `get_page`, `list_pages`, `count_pages`),
    `rebuild_from_disk`, `reconcile`, and `render_markdown`.
    `lies.memory.catalog_models` adds the frozen `CatalogPage` model
    and the `PageSection` enum.
  - `WikiMemoryService.apply_plan` upserts per operation, inside the
    existing flock + atomic-commit envelope — deterministic, no model
    call in the lock. The etl WRITE stage bulk-upserts every written
    path in one transaction (non-fatal; a failed upsert degrades to a
    stderr warning because the catalog is reconcile-cleanable).
  - `lies catalog` CLI group: `status`, `dump`, `reconcile`,
    `rebuild`, `render`. Drift is explicit rather than implicit —
    run `lies catalog reconcile --dry-run` after any out-of-band file
    edit, then drop the flag to apply.
  - `wiki://catalog` and `wiki://catalog/{slug}` MCP resources.
  - `lies status` gains a `catalog: N pages, schema vN` line.
  - Fresh wikis seed `.gitignore` with explicit `.lies/catalog.db`,
    `.lies/catalog.db-wal`, and `.lies/catalog.db-shm` entries
    alongside `.lies/` and `.lies/memory_plans.jsonl`, so the sqlite
    catalog and its WAL siblings stay untracked.
  `wiki/index.md` becomes a read-only markdown derivative rendered on
  demand by `lies catalog render`, listing one title-only
  `- [Title](slug.md)` line per page. Queryable metadata now lives in
  the database rather than in that markdown.
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
- F3 synthesis file-back loop: `lies query --collection NAME [--no-file]
  [--force-file]` (and MCP `collection` / `file` / `force_file`) writes
  a `synthesis` page through `WikiMemoryService.apply_plan` when
  `query_synthesizer_agent` emits `should_file=True`. Filed pages carry
  `derived_from: list[str]` frontmatter. New lint findings
  `synthesis_missing_evidence` and `dangling_derived_from`. Inline
  3-attempt retry on transient persistence errors.
  `SynthesizedMcpAnswer` exposes `should_file` and a serialized
  `file_receipt`; a `WikiPlanInvalid` raised by `run_query` when the
  caller asks for a filing but does not supply `--collection` is
  caught at the CLI (exit 2, spec'd stderr message) and re-raised as
  a typed `ToolError` on the MCP side so LLM callers can react.

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

### Removed

- Implicit "creates collection if missing" promise from `ingest` /
  `sync` short-help text (replaced with accurate description).
- `lies ingest-source <source>` (no `--collection`) is no longer accepted;
  calls fail at the Typer layer with a missing-argument error.
- `indexer` sub-agent (`lies.agents.indexer`, `indexer_agent`,
  `IndexerResult`, `format_log_entry`) — the removal half of the
  F4b + F16 catalog port above. The orchestrator's sub-agent table
  shrinks from five to four (source-reader, page-writer, linter,
  query-synthesizer). `wiki/index.md` maintenance is now owned by
  the sqlite-backed catalog (`<wiki>/.lies/catalog.db`);
  `wiki/log.md` is appended directly by the orchestrator. Operators
  with `"indexer"` in `providers.toml` must remove the line —
  `AGENT_ROSTER` no longer contains it.

### Fixed
- **providers:** `_client_for`, `check_connectivity`, and `_probe` now
  re-read `os.environ` on every call so token rotation is picked up
  without a restart. Missing/empty env vars raise `ProviderConfigError`
  consistently across all three sites (previously `_probe` raised
  `KeyError`). Env var VALUES are never logged, echoed, or included in
  exception messages — only env var NAMES appear in logs and errors.
- Unit suite runtime drops from ~50s to ~14s. The 3 slowest `run_write`
  tests pinned their `atomic_commit` mock to `return_value=None` so the
  post-commit qmd hooks (the dominant cost) skip in tests that do not
  assert on them. `apply_plan` / `apply_repair_plan` tests get an
  autouse fixture that no-ops `_refresh_qmd`. CLI startup-cost tests
  use `sys.executable` instead of `uv run` to skip project-resolution
  overhead. The SIGKILL-escalation test is rewritten as a pure mock
  (no real subprocess). `test_unreachable_when_host_does_not_resolve`
  removed (required a real DNS lookup).
- EPERM + stale heartbeat no longer triggers an unauthorized reap. The
  pid_alive classifier is now tri-state; an EPERM contender with a stale
  heartbeat returns `AcquireResult(status="indeterminate")` which
  callers translate to a new `WikiFlockIndeterminate` exception with an
  operator-actionable message ("Run `lies flock <name> force-repair`").
- F3 synthesis frontmatter: dropped the duplicate `sources:` block
  (synthesis pages have no raw source — they distill other wiki pages
  via `derived_from:`), hand-quoted the `title:` value so questions
  containing `:` or other YAML-significant characters parse cleanly,
  and corrected `collection:` to use the target collection instead of
  `pages_read[0].split('/')[0]` (which yielded `"wiki"` for
  wiki-rooted pages).
- F3 lint shell now reads each synthesis page once instead of three
  times (type detection, `## Evidence` check, and `derived_from`
  resolution all share a single `read_text`).
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
