"""Write stage — placeholder until Task 23."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lies.collections.hash_manifest import HashManifest
    from lies.etl.pipeline import StageResult


def run_write(
    collection: object,
    normalized: object,
    *,
    manifest: HashManifest,
    force: bool,
) -> StageResult:
    """Write normalized documents into the wiki.

    Real implementation lands in Task 23. This stub exists so the
    orchestrator's import surface is complete in Task 20.
    """
    raise NotImplementedError("run_write lands in Task 23")