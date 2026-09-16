# LIES orientation (v{version})

You are attached to the LIES MCP server. Use this payload to orient
yourself. The wiki you are talking to is selected by the
`LIES_WIKI_NAME` env var or the `--name` flag on `lies` CLI commands.

## Paths

- `collections_dir` — `$XDG_CONFIG_HOME/lies/<wiki>/collections/`
  YAML files describing each source collection. One file per
  collection; bootstrapped via `lies sync <name> --source <url>`.
- `wiki_dir` — `$XDG_DATA_HOME/lies/<wiki>/wiki/`
  Derived pages (`<page-type>/<slug>.md`).
- `raw_dir` — `$XDG_DATA_HOME/lies/<wiki>/raw/<collection>/`
  Immutable source mirrors; written by the scrape stage.
- `library_root` — `$XDG_DATA_HOME/lies/library/`
  Shared across wikis; mirrors live here, one directory per
  collection. The library is the source of truth post-cutover.

## Tools

- `init_wiki` — bootstrap a new wiki under the XDG roots.
- `wiki_search` / `wiki_read` — direct memory retrieval.
- `file_knowledge` — write one markdown page.
- `query` / `answer` — synthesized answer; `query` returns
  structured envelope, `answer` returns plain text.
- `lint` — health-check; `fix=True` applies the repair plan.
- `wiki_changes` — recent plan applications.

## Resources (`wiki://`)

- `wiki://status`, `wiki://index`, `wiki://log`,
  `wiki://lint-report`, `wiki://memory-changes`,
  `wiki://catalog`, `wiki://catalog/{{slug}}`,
  `wiki://page/{{path}}`.

## Prompts

- `ask_wiki(question)` — tool-call template: drive `query`.
- `ask_wiki_answer(question)` (slash `/answer`) — drive `answer`.
- `orient(wiki=...)` — reference prose for the four workflows.
- `ingest(source=...)` — `lies sync <name> --source <source>`.
- `query(question=...)` — `query` tool call recipes.
- `lint()` — `lies lint`, `--fix`, repair agent.
- `sync(collection=...)` — `lies sync`, lock envelope.
- `file-back(wiki=...)` — F3 file-back from a query synthesis.

## CLI

- `lies init <name>` — bootstrap wiki + schema.
- `lies sync <collection> --source <url>` — bootstrap + ingest.
- `lies sync` — sync every collection.
- `lies query format` — render an answer as md/table/marp.
- `lies lint [--fix]` — deterministic health-check.
- `lies collections add|modify|list` — manage collection YAMLs.
- `lies mcp up|down|status` — daemon lifecycle.
