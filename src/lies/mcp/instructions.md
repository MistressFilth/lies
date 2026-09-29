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
- `lint` — health-check (unchanged).
- `reindex` — qmd lifecycle (unchanged).

## Prompts (v0.41 surface)

Slash-command entry points surface the following `@mcp.prompt` names.
Hosts that bind slashes to MCP prompts render these under the server
prefix (e.g. `/lies:ask`):

- `ask(question: str)` — synthesized cited answer. Filter tokens
  `+tag`, `-tag` are parsed out of the question text inside the body
  and routed into `tag_expr` / `exclude_tags` on the dispatched tool
  calls (see argument-hint contract).
- `collections(subcommand, args=[])` — library registry CRUD.
- `ingest(source, delete_slug=None, batch_dir=None, dry_run=False)` —
  bring a source into the library.
- `lint(check=None, fix=False)` — health-check the corpus.
- `reindex(reconcile=False, embed=False, force=False, cleanup=False, all_=False)` —
  rebuild the search index.
- `sync(collections=[], no_ingest=False, force=False, dry_run=False, jobs=4, scraper_timeout=300)` —
  pull + ingest remote sources.
- `ground(question: str, top_k=3)` — cite-snippet digest (no
  synthesis). Same filter-token parsing contract as `ask`.

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
