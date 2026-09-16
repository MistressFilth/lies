# LIES orientation (v$version)

You are attached to the LIES MCP server. Wiki: `{wiki}` (or set
`LIES_WIKI_NAME` to scope subsequent calls). Use the four path
facts below as ground truth; do not infer collection storage
from page-write targets.

## Path facts

- `collections_dir` — `$XDG_CONFIG_HOME/lies/<wiki>/collections/`
  YAML files. One per collection; bootstrapped via
  `lies sync <name> --source <url>`.
- `wiki_dir` — `$XDG_DATA_HOME/lies/<wiki>/wiki/`
  Derived pages.
- `raw_dir` — `$XDG_DATA_HOME/lies/<wiki>/raw/<collection>/`
  Immutable source mirrors.
- `library_root` — `$XDG_DATA_HOME/lies/library/`
  Cross-wiki raw mirror.

## Prompt index

- `ingest(source=...)` — full `lies sync` walkthrough.
- `query(question=...)` — `query` tool recipes + citation rules.
- `lint()` — `lies lint [--fix]` flow + repair agent.
- `sync(collection=...)` — `lies sync` envelope + heartbeat.
- `file-back(wiki=...)` — F3 synthesis filing.

The two existing `ask_wiki(question)` and `ask_wiki_answer`
(= slash `/answer`) prompts drive the `query` and `answer`
tools respectively; use them when you only need to ask.

## What NOT to do

Do not write collection storage paths by analogy with the page-write
target `wiki/<collection>/<page-type>/<slug>.md` — that is the
`lies page write` destination, not the source collection location.