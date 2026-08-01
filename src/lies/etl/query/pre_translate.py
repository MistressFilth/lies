"""Structured intent for query translation."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StructuredIntent:
    collection_filter: list[str]
    tag_filter: list[str]
    exclude_terms: list[str]
    body: str
