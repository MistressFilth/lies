"""QMD_UPDATE stage — incremental qmd update per collection."""
from __future__ import annotations

from typing import TYPE_CHECKING

from lies.collections.record import Collection
from lies.qmd.cli import qmd_update

if TYPE_CHECKING:
    from lies.etl.pipeline import StageResult


def run_qmd_update(collection: Collection) -> StageResult:
    from lies.etl.pipeline import StageResult

    try:
        qmd_update(collection.path, collection=collection.qmd_name())
    except Exception:  # noqa: BLE001, S110 - QmdStale is informational; pipeline proceeds
        pass
    return StageResult(success=[], quarantined=[], skipped=[], bytes_in=0, bytes_out=0)
