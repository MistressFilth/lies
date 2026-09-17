# Ingest a source collection (v$version)

Bootstrap a collection YAML + scrape the source URL into the
library + sync into the wiki.

## Walkthrough

1. Pick a wiki (default: `LIES_WIKI_NAME` env var or the only
   registered wiki).
2. Run `lies sync <collection> --source <source> --name <wiki>`.
   The sync helper:
   - `ensure_wiki(name)` — creates the wiki if missing.
   - `bootstrap_library_collection(wiki, name, source)` — writes
     `~/.local/share/lies/library/collections/<name>/config.yaml`.
   - `sync_collection(wiki, name)` — scrape → normalize → map →
     write → commit (one git commit per run).
3. Inspect the result: `lies library list` to confirm the
   config exists; `find ~/.local/share/lies/library/collections/<name>`
   to confirm the raw mirrors landed.

## Pitfalls

- `--source` mismatches an existing config's `source` field →
  `CollectionMismatch`. Use `lies library modify --set
  source=...` to change an existing source.
- Concurrent syncs collide on the wiki's flock + atomic-commit
  envelope; `lies flock <wiki> status` shows the holder.
