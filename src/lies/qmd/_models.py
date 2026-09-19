"""Pydantic models returned by qmd library functions."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ReindexResult(BaseModel):
    """Outcome envelope for ``qmd_reindex``.

    Each flag indicates whether the corresponding stage ran successfully.
    ``errors`` carries human-readable failure strings; an empty list means
    success across all stages.
    """

    reconciled: bool = False
    indexed: bool = False
    embedded: bool = False
    cleaned: bool = False
    errors: list[str] = Field(default_factory=list)
