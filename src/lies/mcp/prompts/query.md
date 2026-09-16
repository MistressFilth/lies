# Query the wiki (v$version)

Synthesize an answer from the wiki + library via the `query` MCP
tool.

## Walkthrough

1. Call `mcp__lies__query(name=<wiki>, question=<q>)`. The
   tool returns a structured envelope:
   `{answer, fallback_used, fallback_reason, citations,
   pages_read, changed_pages, synthesis_used, should_file,
   file_receipt}`.
2. `answer` is the synthesized body; `citations` are the
   source-grounding references (`[library]` or `[wiki]`
   discriminator).
3. If `fallback_used=true`, mention it in the user-visible
   answer — the answer came from the index fallback, not qmd
   search.

## Pitfalls

- `query` vs `answer`: `answer` returns the body verbatim
  (chat-friendly); `query` returns the structured envelope.
  Pick `answer` when the response should render in chat; pick
  `query` when you need citations or fallback metadata.
- `should_file=true` from the synthesis; use the `file-back`
  prompt for that path.
