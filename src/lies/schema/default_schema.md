# LIES Default Schema

This is the default schema for a LIES wiki. It tells the agent how to
organize, ingest, query, and lint the wiki. Per Karpathy: "you and the
LLM co-evolve [the schema] over time." Copy this file to
`<wiki>/.lies/schema.md` and edit as your wiki matures.

## Page types

The wiki supports the following page types. Each page lives at
`wiki/<page-type>/<name>.md` (e.g., `wiki/entities/alice.md`).

- **overview** — the top-level synthesis. One per wiki, at
  `wiki/overview.md`. Always keep up to date.
- **entity** — a person, place, project, system, or other named thing
  mentioned by the corpus. Example: `wiki/entities/postgres.md`.
- **concept** — an abstract idea, pattern, framework, or methodology.
  Example: `wiki/ concepts/consensus.md`.
- **comparison** — a side-by-side of two or more entities/concepts.
  Example: `wiki/comparisons/postgres-vs-mysql.md`.
- **source** — a summary of a single raw source, with links to the
  pages it informed. Example: `wiki/sources/karpathy-llm-wiki.md`.

## Frontmatter

Every page has YAML frontmatter:

```yaml
---
title: "Concise title"
type: entity | concept | comparison | source | overview
tags: [optional, list, of, tags]
created: YYYY-MM-DD
updated: YYYY-MM-DD
sources:
  - raw/articles/some-article.md
---
```

## Ingest workflow

When ingesting a source:

1. **Read** the source (markdown, plain text; PDF/URL via WebSearch).
2. **Extract** claims, entities, concepts, comparisons.
3. **Plan** page operations: which new pages, which updates, in what order.
4. **Write** pages via CodeMode, in parallel where independent.
5. **Update** `wiki/index.md` (content catalog) atomically with the writes.
6. **Append** a `## [YYYY-MM-DD] ingest | <Title>` entry to `wiki/log.md`.
7. **Reindex** via `qmd update`.
8. **Commit** the changes as one git commit (message = log entry).

A single ingest may touch 10–15 pages.

## Query workflow

When answering a question:

1. **Search** via `qmd query` (hybrid BM25 + vector + rerank).
2. **Read** the top-N pages (default 5) via `qmd get`.
3. **Synthesize** an answer with inline citations
   (`[page-name](path:line)`).
4. **Offer** to file the answer as a new page if it's worth keeping
   (per Karpathy: "good answers can be filed back").

## Lint workflow

Periodically health-check the wiki. Look for:

- **Contradictions** between pages.
- **Stale claims** superseded by newer sources.
- **Orphan pages** with no inbound links.
- **Missing pages** for entities/concepts mentioned but not yet covered.
- **Missing cross-references** that should exist.
- **Data gaps** that a web search could fill.

Write findings to `wiki/lint-report.md`. Each lint run appends a
`## [YYYY-MM-DD] lint | N findings` entry to `wiki/log.md`.

## Invisible memory

After every turn, the orchestrator checks whether the main agent
searched or read the wiki, or whether the user supplied clear
project source material. When it does, the MemoryEnricher proposes
a structured `MemoryPlan`; the host validates and applies it
through `WikiMemoryService` and emits a git commit.

Rules:

- Memory captures only durable project knowledge (facts, source
  claims, concepts, contradictions, crosslinks). It never captures
  user preferences, working decisions, or task history.
- Every operation requires an evidence reference.
- Updates and appends carry the current page's content hash. A
  mismatch causes a fresh read and a single enrichment retry.
- Source files in `raw/` are immutable and never written.
- Deletions and renames are not part of ordinary-turn memory. They
  are reserved for explicit maintenance flows.
- Receipts surface only material page changes, conflicts, or
  persistence failures. Routine reads and bookkeeping stay
  silent.
