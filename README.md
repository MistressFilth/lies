# LIES

**Library of Inconsistent Explanations & Sources**

A Karpathy-pattern LLM wiki — a pydantic-ai-harness agent that maintains a git-backed wiki of interlinked markdown files over a corpus of raw sources. The schema (a per-wiki markdown file) defines page types, conventions, and workflows. The human curates sources and asks questions; the agent does all bookkeeping.

## Quick start

```bash
uv sync
uv run lies init ./my-wiki
uv run lies ingest ./my-wiki/raw/articles/some-article.md
uv run lies query "What do my sources say about X?"
uv run lies lint
```

See `docs/superpowers/specs/2026-07-27-lies-design.md` for the full design.