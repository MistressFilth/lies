# Sync a collection (v$version)

Run the deterministic ingest pipeline against an existing
collection YAML.

## Walkthrough

1. `lies sync <collection> --name <wiki>` runs:
   scrape (`run_scrape`) → normalize (`run_normalize`) →
   map (`run_map`) → write (`run_write`) → commit.
2. The pipeline takes a heartbeat + create-lock
   (`wiki.sync_create_lock_path`); concurrent syncs return
   `WikiLockBusy` unless `--force`.
3. After sync, `wiki/log.md` gets a new entry; the qmd derived
   index refreshes.

## Multi-collection mode

- `lies sync` (no positional arg) iterates every collection
  YAML in the wiki and chains `qmd update` + `qmd embed` after
  the sync loop. The auto-chain replaces the separate
  `/reindex` invocation operators previously had to run by
  hand.
- `lies sync --skip-reindex` opts out of the qmd chain. CI
  matrices that reindex separately preserve the pre-chain
  behavior.
- `lies sync --reconcile` runs sync per collection before the
  qmd reindex.

## Pitfalls

- `WikiFlockIndeterminate`: heartbeat held by an unkillable
  process. Operator runs `lies flock <wiki> force-repair`.
