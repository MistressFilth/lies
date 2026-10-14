# LIES

[![CI](https://github.com/MistressFilth/lies/actions/workflows/ci.yml/badge.svg)](https://github.com/MistressFilth/lies/actions/workflows/ci.yml)

**Library of Inconsistent Explanations & Sources**

Most LLM-document tools work like RAG: drop sources in, retrieve chunks at
query time, regenerate an answer. Nothing accumulates. Ask a subtle question
that needs five sources synthesized and the model pieces together the same
fragments every time.

LIES is different. After every source you add and every question you ask, an
agent reads what changed, extracts the key information, and quietly updates
the wiki — entity pages, concept summaries, cross-references, contradictions.
The wiki is a persistent, compounding artifact. You never (or rarely) write
the wiki yourself; the agent maintains it.

Three layers: **raw/** (your curated sources, immutable), **wiki/** (the
agent's markdown; LLM-owned), **schema.md** (the contract that tells the
agent how to behave). You curate and ask; the agent does the bookkeeping.

After a question, a `MemoryEnricher` sub-agent proposes a structured
`MemoryPlan` only when evidence warrants it; the host validates it and
applies it through `WikiMemoryService`, which writes the page, rebuilds the
index, appends the log, and commits atomically. Material changes surface in
a small receipt at the end of the turn. Memory captures durable project
knowledge — never user preferences, working decisions, or task history.
Inspect it with `lies memory`; `lies memory reconcile` rebuilds from
`git log` if the sidecar drifts.

## Quick start

```bash
uv sync
uv run lies ingest --source https://pydantic.dev/docs/ai/llms.txt --collection pydantic-ai
uv run lies query "What do my sources say about X?"
uv run lies lint
```

The first `ingest --source` writes a deterministic mirror under the library
(`$XDG_DATA_HOME/lies/library/collections/<name>/`) and registers it in the
library catalog. Use `--batch <DIR>` to walk a directory of sources into one
collection, `--exclude-stem` / `--exclude-dir` to skip noise. Ingest is
deterministic (no LLM call). The source directory must be *outside* the
collection it writes to — pointing `--batch` at a collection directory
cannot converge, so `ingest` refuses it with exit 2.

Launch the REPL (no subcommand) for an interactive session:

```bash
uv run lies
lies> /help
```

## Using LIES from Claude Code

LIES ships an MCP server. Register it with Claude Code once and the library
becomes available as tools in any Claude Code session:

```bash
# Register for the current user, defaulting to wiki name "default":
claude mcp add --transport stdio lies -- uv run --project . lies mcp

# Pin a specific wiki by setting LIES_WIKI_NAME:
claude mcp add --transport stdio --env LIES_WIKI_NAME=my-research \
    lies -- uv run --project /path/to/lies lies mcp
```

To keep a warm server running independently of any host, run it as a daemon:

```bash
lies mcp up                      # detached; prints the URL
claude mcp add --transport http lies http://127.0.0.1:8737/mcp
lies mcp status                  # pid, URL, uptime, log path
lies mcp down                    # stop it (also stops qmd; --no-qmd keeps it)
```

`lies mcp up` binds `127.0.0.1:8737` by default (`--port` / `--host` to
change). The daemon is per-wiki — pidfile at
`$XDG_RUNTIME_DIR/lies/<name>/mcp.pid`, log at
`$XDG_STATE_HOME/lies/<name>/mcp.log`. `lies mcp down` only stops daemons
that `up` recorded, so it never kills a live stdio session. The daemon is
machine-global, so `down` disconnects every client on the host at once; it
says so on stderr and stops anyway. `--force` is accepted and gates nothing.

`lies mcp up` also ensures qmd's own daemon is running (qmd is the search
backend; `--no-qmd` skips it). When qmd is unreachable mid-session,
`QmdCapability` falls back to an in-process server and tags every search
result `degraded: True` so the model knows it was not qmd-backed.

## MCP tools

Once registered, a host sees:

- `collections_read(subcommand, name?)` — live library registry.
  `list` (one row per collection), `tag_list` (tag → collections map),
  `info` (one collection's metadata; requires `name`).
- `search(question, tag_expr?, exclude_tags?, hypothetical?)` — single-batch
  hybrid vec+lex query. Returns `hit`/`hits` with snippets, plus
  `searched_scope` and `unknown_tags`.
- `read(paths)` — verbatim page bodies, one `get` per path. Paths qmd could
  not resolve come back under a synthetic `_missing` key; a batch is never
  silently narrowed.
- `lib_ask(question, tag_expr?, exclude_tags?, format?)` — synthesized,
  inline-cited answer (`[[slug]]: "verbatim text"` per claim).
- `lint(name?, check?, fix?)` — corpus health report; `--fix` applies the
  safe repairs.
- `reindex(name?, all?, embed?, force?, ...)` — rebuild the qmd index.
  Destructive flags: `all` and `cleanup` (both confirm).

### `search` return fields

- `hit` / `hits` — ranked results with snippets and scores.
- `searched_scope` — the collections actually searched (every registered
  collection when untagged). Never widened on a filter error.
- `unknown_tags` — unresolvable `tag_expr` atoms, reported whole.
- `no_coverage` — true when qmd ran and found nothing. A claim about the
  **corpus**.
- `transient` — true when the dispatch did not finish (wedge, deadline,
  error). A claim about the **run**, not the corpus. Retry once; if it stays
  transient, report the lookup as inconclusive rather than as empty.
- `fallback_reason` — why the dispatch returned no hits; carries qmd's own
  last output on a timeout.

The per-call deadline is 60s (`LIES_QMD_FANOUT_TIMEOUT`); liveness probes
keep a short one, because a slow answer to "is this alive?" is itself the
failure.

## Tag-filter language

Filter to library collections. `&` binds tighter than `|`; `-tag` excludes;
quoted tags (`+"airflow provider"`) allow spaces.

```bash
lies query +airflow what are DAGs?
lies query +airflow&amazon -python what connectors?
lies query --tag-expr "airflow|spark" --exclude-tag aws what is X?
```

**The library is the source of truth for collections.** Collections live as
directories under `$XDG_DATA_HOME/lies/library/collections/<name>/`; wikis
do not own collections.

### Qualifier prefixes (`t:` / `c:`)

The two prefixes select different things.

- **`c:foo`** — the library collection *named* `foo`, and nothing else. A
  tag that happens to read `foo` matches nothing here.
- **`t:foo`** — every collection carrying the tag `foo`, plus any collection
  *named* `foo`: a collection's own name is implicitly one of the tags that
  match it. A bare atom `foo` means the same thing as `t:foo`.
- `|` unions the two sides' collection sets, `&` intersects them.

So `t:mcp` matches the `fastmcp` collection through its `mcp` tag — no
collection is named `mcp`, so `c:mcp` matches nothing. And `c:opencode`
matches the `opencode` collection even though nothing tags it `opencode`.

An atom in neither vocabulary is reported in `unknown_tags` with an empty
`searched_scope`; the scope is refused, never widened to the whole library.

`lib_ask` accepts the same atoms via `tag_expr` / `exclude_tags`.

## Configuration

Environment variables:

- `LIES_WIKI_NAME` — wiki name (default: `default`)
- `LIES_LANG` — wiki language (default: `en`)
- `LIES_LOG_LEVEL` — log level when logfire is inactive (default: `INFO`)
- `LOGFIRE_TOKEN` — enables logfire + `pydantic-ai` instrumentation
- `LIES_QMD_TRANSPORT` — `http` (default, uses the qmd daemon) or `stdio`
  (a `qmd` process per agent)
- `LIES_QMD_URL` — qmd daemon URL (default: `http://localhost:9000/mcp`).
  The port is derived from this value, so every qmd call site addresses the
  same daemon. The host is the name `localhost` because that is what qmd
  binds, so LIES follows whatever address family the host resolves it to.
- `LIES_QMD_LOCK_PATH` — override the qmd flock path (sandboxing)
- `LIES_QMD_QUERY_LOCK_WAIT` — query-gate wait budget in seconds
  (default: `120`)
- `LIES_XDG_DATA_HOME` / `_CONFIG_HOME` / `_RUNTIME_DIR` / `_STATE_HOME` /
  `_CACHE_HOME` — override the XDG roots for LIES
- `LIES_*_MODEL` (per-agent, e.g. `LIES_ORCHESTRATOR_MODEL`) — override a
  model's selection; a non-empty value beats `providers.toml`.

Most subcommands accept `--name` to override the wiki for one invocation.
`lies config` prints the active model and wiki.

## Development

`make check` runs lint + typecheck + format + unit tests; `make test`
adds the integration suite. The **CI gate** is
`INTEGRATION=1 CI=true pytest tests/ --runslow` — a plain `pytest` reports
integration as skipped and hides the entire slow surface, so a green local
run is not evidence. See `AGENTS.md` for the agent-facing contracts and
known flakes.

## License

MIT — see `LICENSE`.

## Project links

- Issues: https://github.com/MistressFilth/lies/issues
- Changelog: `CHANGELOG.md`
- Agent contracts and known flakes: `AGENTS.md`