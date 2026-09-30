# LIES orientation (v$version)

You are attached to the LIES MCP server. Use this payload to orient
yourself. The wiki you are talking to is selected by the
`LIES_WIKI_NAME` env var or the `--name` flag on `lies` CLI commands.

## Paths

- `library_dir` — `$XDG_DATA_HOME/lies/library/`
  Collection configs at `library/collections/<name>/config.yaml`.
  Source docs under `library/collections/<name>/pages/` or wherever
  the scraper wrote them.

## Tools (v0.40 surface)

- `collections_read(subcommand, name?)` — live registry reader.
  Subcommands: `list` (one row per collection), `tag_list` (tag → collections
  map, qualifier prefix stripped), `info` (single collection's full metadata,
  requires `name`).
- `search(question, tag_expr?, exclude_tags?, hypothetical?)` —
  single-batch hybrid vec+lex qmd query. Returns top-1 + top-10 ranked hits
  with snippets, plus `searched_scope` and `unknown_tags`.
- `read(paths)` — verbatim page bodies. Wiki page IDs route to
  `memory_service.read`; library paths (`<collection>/<page>`) route to
  `qmd_get`. Source-aware dispatch.
- `lib_ask(question, tag_expr?, exclude_tags?, file_back?)` — prose answer.
  Calls the librarian subagent (Classify → Search → Read → Return 4-step
  pipeline), then the synthesizer subagent. Returns a `SynthesizeEnvelope`
  with `answer`, `citations`, `pages_read`, `searched_scope`,
  `fallback_used`, `synthesis_used`, `fallback_reason`.
- `lint(name?, check?, fix?, force_repair?)` — health-check. `check`
  narrows the report to one finding category (`orphan`, `missing_xref`,
  `missing_page`, `missing_required_section`, `dangling_derived_from`,
  `contradiction`, `stale`, `data_gap`), matched case-insensitively
  with an optional plural. `fix=True` applies the repair plan; with
  `check` set, the repair is scoped to that category too.
- `reindex` — qmd lifecycle. `cleanup` / `all_` are destructive and
  elicit confirmation.
- `list_prompts()` — the prompt inventory as JSON, with each prompt's
  argument names and descriptions.
- `get_prompt(name, arguments?)` — renders the named prompt and
  returns its messages.

## Prompts (v0.42 surface)

Seven `@mcp.prompt` names are registered. Reach them with `get_prompt`:

    get_prompt(name=<prompt>, arguments={<the single string param>: "<tail>"})

**Prefer `get_prompt` over the slash form for every user question.**
Tool-call arguments are not pre-tokenized, so the full string arrives
intact. The slash form (`/lies:sync all`, `/lies:lint --fix`) is
single-token only: hosts pre-tokenize the slash tail on whitespace and
bind tokens positionally to declared parameters, so everything after the
first token is overflow and is dropped. `/lies:ask +c:opencode why`
leaves the body asking for a question, because `why` never arrives.

Every prompt takes exactly one `str` and parses its own flags. A typed
parameter past position one would receive a bare word from the slash
tokenizer and fail JSON decode, which is why there are no `bool` / `int`
/ `list[str]` parameters on the prompt surface. Flag values bind as
`--flag=value` or `--flag value`; a value flag given no value renders an
error body rather than a command. A boolean written `--flag=value` is
still set — the value is discarded and the body says so.

- `ask(question: str)` — synthesized cited answer. `+tag` / `-tag`
  filter tokens are parsed out of the **leading run** of `question`
  inside the body and routed into `tag_expr` / `exclude_tags` on the
  dispatched calls.
- `ground(tail: str)` — cite-snippet digest (no synthesis). Same
  filter-token contract, plus `--top_k N` (clamped to [1, 10]).
- `collections(tail: str)` — registry CRUD. `<subcommand> <args…>`.
  Subcommands: `list`, `show` (alias `info`), `new` (alias `add`),
  `modify`, `delete` (alias `remove`), `where`, `enrich-tags`, `tag`
  (`<slug> <tag…>`, rendered as `modify --tag`), `bootstrap-all`
  (alias `register-shipped`). Flags: `--source` / `--prompt` on
  `new`; `--tag` / `--untag` / `--set` / `--from-file` on
  `modify`; `--json` on `list`.
- `ingest(tail: str)` — `<source>` or `--batch <dir>`, plus
  `--source` / `--collection` / `--slug` / `--title` / `--slug-prefix`
  / `--exclude-stem` / `--exclude-dir` / `--force` / `--dry-run`.
  A bare path binds to `--source`. There is no `--delete`: nothing in
  the CLI removes an ingested page, so a tail that asks for one gets
  the two things that do exist rather than a command that exits 2.
- `lint(tail: str)` — `--check <name>` (one finding category; the
  `lint` tool's `check` parameter filters the report to it), `--fix`.
- `reindex(tail: str)` — `--reconcile` / `--embed` / `--force` /
  `--cleanup` / `--all` / `--name <wiki>`. `all`, `all_` and `--all`
  are the same destructive marker; `cleanup` and `all_` elicit
  confirmation. `--name` takes exactly one token, so
  `--name pydantic all` reindexes `pydantic` with `all_=True`.
- `sync(tail: str)` — `<collection…>` or `all` (every collection with
  a scraper), plus `--source` / `--name` / `--force` / `--wait` /
  `--fail-busy` / `--wizard` / `--skip-reindex`. `lies sync` takes one
  positional, so several names render one command each.

## Routing rules

**A user message carrying `+tag` include tokens (e.g. `+c:opencode …`)
or `-tag` exclude tokens (e.g. `-t:draft …`) goes through `get_prompt`,
not `lib_ask`.** `lib_ask` is the synthesizer inside the `ask` prompt's
body, not a user entry point; calling it directly skips the filter
parsing and the citation-render instructions. Call `lib_ask` only when a
prompt body has already routed you to it, or when you are debugging the
synthesizer itself.

    get_prompt(
        name="ask",      # synthesized cited answer
        # or name="ground"  # verbatim snippet digest, no synthesis
        arguments={"question": "<the full user message, filters included>"}
    )

The prompt body parses `+tag` / `-tag` out of the **leading run** of
`question` and routes the typed filter into `tag_expr` / `exclude_tags`
on the dispatched calls. Two guards keep ordinary English out of the
filter path: a token is read as a filter only while no question word
has been seen yet (the documented shape is `+tag -tag <question>`), and
only when what follows the sigil looks like a tag — a `:` qualifier
(`c:opencode`) or two or more characters. So `-1`, a bare `--`, a lone
`-`, and a question *about* option flags ("what does `-e` do", "is `-p`
or `-q` faster") all stay in the question text.

## Workflow

1. `collections_read("list")` to discover what collections exist.
2. `search(...)` with the user's question + filter to get ranked hits + snippets.
3. `read(paths)` to deep-read the pages whose snippets look most relevant.
4. `lib_ask(...)` to compose a cited answer from the librarian's excerpt bundle.

`lib_ask` does steps 1–4 internally — the librarian subagent picks reads based on snippets. Use the individual tools when you need finer control.

## Tag-filter syntax

`tag_expr` body (no leading sigil):
- `c:<name>` — collection qualifier
- `t:<tag>` — tag qualifier
- `|` — OR (lower precedence)
- `&` — AND (higher precedence)

Examples:
- `c:opencode` → opencode collection only
- `c:opencode|c:claude_code` → either
- `c:opencode&t:linux` → opencode pages tagged `linux`

Caller-supplied filter is a CONSTRAINT, not a hint — the librarian intersects
the user's filter with the matched registry tokens, then searches the union.
