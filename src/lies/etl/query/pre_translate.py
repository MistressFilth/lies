"""Structured intent for query translation."""
from __future__ import annotations

import re
from dataclasses import dataclass

from lies.etl.cost import CostBudget


@dataclass(frozen=True)
class StructuredIntent:
    collection_filter: list[str]
    tag_filter: list[str]
    exclude_terms: list[str]
    body: str


_OPERATOR_RE = re.compile(r"[+\-|]")


def _looks_like_query(question: str) -> bool:
    return bool(_OPERATOR_RE.search(question))


def pre_translate(question: str, *, model: object, budget: CostBudget) -> StructuredIntent:
    """Translate natural-language question into StructuredIntent.

    Returns a noop intent when no operators are detected (no LLM call).
    Otherwise invokes the model (budget-tracked) and parses structured
    output.
    """
    noop = StructuredIntent(
        collection_filter=[], tag_filter=[], exclude_terms=[], body=question
    )
    if not _looks_like_query(question):
        return noop
    budget.spend(calls=1)
    # Placeholder LLM call; in practice uses pydantic_ai Agent with
    # StructuredIntent output type. Kept minimal here; full agent wired
    # in follow-up.
    del model
    return noop
