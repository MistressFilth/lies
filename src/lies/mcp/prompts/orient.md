# LIES orientation (v$version)

You are attached to the LIES MCP server. Wiki: `{wiki}` (or set
`LIES_WIKI_NAME` to scope subsequent calls). Use the four path
facts below as ground truth; do not infer collection storage
from page-write targets.

## Path facts

- `wiki_dir` — `$XDG_DATA_HOME/lies/<wiki>/wiki/`
  Derived pages.
- `raw_dir` — `$XDG_DATA_HOME/lies/<wiki>/raw/<collection>/`
  Immutable source mirrors.

Collections are global library artifacts. Each named unit lives at
`$XDG_DATA_HOME/lies/library/collections/<slug>/` and contains the
scraped content plus a `config.yaml` (source URL, tags, scraper
settings). No per-wiki collection config exists; any wiki can read any
library collection on demand. New collections land via
`lies ingest --source <URL> --collection <slug>` (writes content +
bootstraps config) or `lies library new <slug> --source <URL>` (config
only).

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

Do not write collection storage paths by analogy with any other
wiki-side surface. The page-write target (the destination of
`lies page write`) is a separate concern from where source
collection configs and raw mirrors live — use the path facts
above as the only source of truth. Collections live in the
library, not in wikis; there is no per-wiki collection
directory anymore.