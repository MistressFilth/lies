"""Deterministic translation of StructuredIntent to QMD query syntax."""

from __future__ import annotations

from lies.etl.query.pre_translate import StructuredIntent


def translate(intent: StructuredIntent) -> str:
    parts: list[str] = []
    if intent.collection_filter:
        parts.append("+" + "|".join(intent.collection_filter))
    for tag in intent.tag_filter:
        parts.append(f"+tag:{tag}")
    for term in intent.exclude_terms:
        parts.append(f"-{term}")
    parts.append(intent.body)
    return " ".join(parts)
