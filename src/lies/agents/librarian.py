"""Librarian subagent — staged retrieval dispatch (F18).

Port of the ask `librarian-prompt.md` 4-step contract (classify →
search → read → return bundle) into a pydantic-ai in-process
subagent. The orchestrator routes retrieval through this agent;
the synthesizer consumes ``LibrarianOutput`` to compose the cited
answer.

Unlike ask's harness-``Agent``-tool dispatch, lies runs in-process
via ``librarian_agent.run_sync(deps)``. No background task, no
host-loop await — the orchestrator's ``run_query`` blocks until
the librarian returns.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic_ai import Agent
from pydantic_ai.models import Model

from lies.agents.base import make_sub_agent
from lies.markdown_spans import Span


@dataclass(frozen=True)
class LibrarianDeps:
    """Dependencies the librarian subagent needs to retrieve excerpts.

    Attributes:
        question: The user's natural-language question.
        tag_expr: Body of a single include token (no leading sigil),
            e.g. ``'a&b|c'``. ``None`` for untagged queries.
        exclude_tags: NOT tags without leading sigil.
        top_k: Maximum number of excerpts to return (default 5).
    """

    question: str
    tag_expr: str | None
    exclude_tags: list[str]
    top_k: int = 5


@dataclass(frozen=True)
class PageExcerpt:
    """A page excerpt the librarian curated for the question.

    ``spans`` (F19) carries the structured span view rather than a
    raw text blob — the synthesizer picks per-claim span from this
    list.
    """

    collection: str
    slug: str
    title: str
    spans: list[Span]


@dataclass(frozen=True)
class LibrarianOutput:
    """The librarian's evidence bundle — verbatim passages the
    synthesizer composes an answer from.

    The librarian does NOT write the answer; the main agent holds
    the cited synthesis.
    """

    tag_expr: str | None
    exclude_tags: list[str]
    excerpts: list[PageExcerpt]
    distinct_pages: int


# Validator workaround — qmd's validateSemanticQuery guard rejects
# in-word hyphens in vec queries. The librarian rewrites compound
# words as multi-word phrases BEFORE posting to qmd.
_HYPHEN_PHRASES: dict[str, str] = {
    "error-handling": "error handling",
    "comma-separated": "comma separated",
    "case-insensitive": "case insensitive",
    "well-defined": "well defined",
    "thread-safe": "thread safe",
}
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_MONTHS = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]


def _rewrite_query_for_validator(query: str) -> str:
    """Rewrite compound words + ISO dates so qmd's vec guard accepts the query.

    The qmd search daemon's ``validateSemanticQuery`` rejects any
    hyphen immediately followed by a word character in vec text as
    suspected negation syntax. This rewrite is conservative — over-
    rewriting is safe; under-rewriting wedges the daemon.

    Args:
        query: The raw question text.

    Returns:
        The query with known compound words expanded and ISO dates
        spelled out.
    """
    out = query
    for hyphenated, expanded in _HYPHEN_PHRASES.items():
        out = out.replace(hyphenated, expanded)
    out = _ISO_DATE_RE.sub(
        lambda m: f"{_MONTHS[int(m.group(2)) - 1]} {int(m.group(3))} {m.group(1)}",
        out,
    )
    return out


LIBRARIAN_SYSTEM_PROMPT = """You are the librarian subagent for the LIES wiki.

Your job is to classify the user's question, retrieve relevant
excerpts, and return a curated evidence bundle. You do NOT write
the answer — the parent turn composes the cited synthesis from
your bundle.

## 1. Classify (registry-driven)

Discover which collections the question touches by reading the LIVE
wiki registry — never a hardcoded map.

1. Call `wiki_catalog` to list every collection. Each entry carries
   `name`, `tags`, and `scope_keywords`.
2. Match the question's tokens against those three surfaces.
3. Build `tag_expr` as a `|`-union of matched tags.
4. When nothing intersects, run UNTAGGED (`tag_expr=None`). An empty
   registry is also untagged; note it in the bundle.
5. Pass the caller's `exclude_tags` through to `wiki_search`
   unchanged so the daemon enforces the exclusion site-side.

## 2. Search

Call `wiki_search` with the question and the `tag_expr` from step 1.
`wiki_search` is the daemon-backed retrieval surface. It resolves
`+tag`/`-tag` tokens internally.

- `unknown_tags` non-empty → hard error: surface it to the caller.
  Do NOT silently retry without tags.
- `no_coverage=True` (or `searched_scope` landed off-domain) → a
  scope miss: consult `searched_scope`, re-read the registry, re-
  scope `tag_expr`, retry once.

## 3. Read (curated excerpts)

Take the top-K hits from `wiki_search`'s `hits` field (highest
`score` first), call `wiki_read` on each, and select the sections
relevant to the question. Return a few hundred words per page —
the verbatim passage that can back a claim — not the whole body.

On a read failure, log and continue with the rest. If every hit
fails, fall back to the `wiki_search` tool and excerpt from its
`pages_read`.

## 4. Return

Emit a JSON evidence bundle:

```json
{
  "tag_expr": "<chosen union, or null when untagged>",
  "exclude_tags": [...],
  "excerpts": [
    {"collection": "<collection>", "slug": "<docid>", "title": "<page title>", "spans": [<Span objects>]}
  ],
  "distinct_pages": <int>
}
```

A claim with no returned excerpt is a signal for the parent turn
to issue a follow-up, not to fabricate.
"""


def librarian_agent(
    model: Model | str = "anthropic:claude-opus-4-7",
) -> Agent[LibrarianDeps, LibrarianOutput]:
    """Construct the librarian subagent.

    Tools: ``wiki_search``, ``wiki_read``, ``wiki_catalog``. Defined
    by the orchestrator's tool registry — see
    ``Orchestrator._register_librarian_tools``.

    Dispatches via ``run_sync(deps)`` (in-process, not harness
    await-async).
    """
    agent: Agent[LibrarianDeps, LibrarianOutput] = make_sub_agent(
        model=model,
        output_type=LibrarianOutput,
        deps_type=LibrarianDeps,
        system_prompt=LIBRARIAN_SYSTEM_PROMPT,
    )
    return agent
