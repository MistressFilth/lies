"""Normalize stage — placeholder until Task 22."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lies.etl.pipeline import StageResult
    from lies.scrapers.base import ParsedDoc


def run_normalize(collection: object, parsed_docs: list[ParsedDoc]) -> StageResult:
    """Normalize parsed documents into canonical markdown.

    Real implementation lands in Task 22. This stub exists so the
    orchestrator's import surface is complete in Task 20.
    """
    raise NotImplementedError("run_normalize lands in Task 22")