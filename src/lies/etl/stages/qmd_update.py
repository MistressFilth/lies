"""QMD update stage — placeholder until Task 24."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lies.etl.pipeline import StageResult


def run_qmd_update(collection: object) -> StageResult:
    """Refresh the QMD derived index.

    Real implementation lands in Task 24. This stub exists so the
    orchestrator's import surface is complete in Task 20.
    """
    raise NotImplementedError("run_qmd_update lands in Task 24")