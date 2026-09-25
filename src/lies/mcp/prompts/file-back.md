# File a synthesis back to the wiki (v$version)

**Status (this release):** the file-back path is deferred. The
`/file-back` slash command still resolves, but the underlying
mechanism (`mcp__lies__synthesize(file_back=True)`) raises a
`ToolError` with the message `"file_back is deferred in this
release; see superpowers/specs/2026-09-24-library-mode-read-side-rewrite-design.md"`.

## Why this prompt still exists

The `/file-back` slash command registration is kept so callers
get a graceful explanatory message instead of an "unknown tool"
error when they reach for it during the deprecation window. The
prompt body documents the deferred state and points operators at
the spec that scopes the future write-tool work.

## What the user should do in the meantime

- Wiki writes go through `lies page write --collection <name>
  --type <type> --slug <slug> --title "..." --body-file <path>`
  from the CLI. The `file_knowledge` MCP writer is retired in
  the library-mode read-side rewrite (the current MCP surface is
  read-only).
- Re-run the synthesis with `synthesize(file_back=False)` (the
  default) to get the prose answer; filing-back lands in a
  separate write-tool spec, not in this release.

## Reference

- Spec: `superpowers/specs/2026-09-24-library-mode-read-side-rewrite-design.md`
  → "File-back — Defer. `synthesize()` accepts `file_back=True`
  and raises `ToolError` (\"deferred\")."
- Pre-rewrite behavior this prompt used to describe (F3 file-back
  walkthrough, `WikiMemoryService.apply_plan`, `WikiWriteConflict`
  / `WikiCommitFailed` retry envelope) is dormant until the
  write-tool spec lands.
