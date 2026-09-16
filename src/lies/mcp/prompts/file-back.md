# File a synthesis back to the wiki (v$version)

F3 file-back path: when `mcp__lies__query` returns
`should_file=true`, the synthesized answer earns a wiki page.

## Walkthrough

1. The synthesizer returns `file_receipt` in the `query`
   response envelope; the receipt carries `path`,
   `commit_sha`, and any conflict / skip reason.
2. The page lands at `wiki/<collection>/synthesis/<slug>.md`
   with `derived_from:` listing the pages that fed it.
3. `WikiMemoryService.apply_plan` is the single owner of the
   write; it validates the plan, snapshots the working tree,
   commits atomically, restores on failure, refreshes qmd.

## Pitfalls

- `WikiWriteConflict`: page hash mismatch (someone wrote the
  page since the synthesis ran). Retry the synthesis, then
  re-file.
- `WikiCommitFailed`: git-level failure (network, hooks).
  Retry the file-back; the snapshot/restore envelope protects
  the working tree.
