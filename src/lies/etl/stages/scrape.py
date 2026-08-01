"""Scrape stage — placeholder until Task 21."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lies.etl.pipeline import StageResult


def run_scrape(collection: object) -> StageResult:
    """Scrape the source and return parsed documents.

    Real implementation lands in Task 21. This stub exists so the
    orchestrator's import surface is complete in Task 20.
    """
    raise NotImplementedError("run_scrape lands in Task 21")