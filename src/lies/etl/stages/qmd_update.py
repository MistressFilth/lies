"""QMD_UPDATE stage — refresh the qmd derived index for the wiki at `cwd`.

`qmd update` reindexes every collection registered under the working
tree in one pass; it does not accept a per-collection flag, so this
stage is necessarily a full reindex from the caller's perspective.
Per-collection refresh must be done by configuring qmd itself (e.g.
glob patterns), not by passing CLI flags through this wrapper.

The qmd derived index is non-authoritative: a refresh failure must
not invalidate the wiki git commit that already succeeded. We swallow
every exception so the pipeline proceeds; the next sync retries the
refresh.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from lies.collections.record import Collection
from lies.qmd.cli import qmd_update

if TYPE_CHECKING:
    from lies.etl.pipeline import StageResult


def run_qmd_update(collection: Collection) -> StageResult:
    from lies.etl.pipeline import StageResult

    try:
        qmd_update(collection.path)
    except Exception:  # noqa: BLE001, S110 - qmd is derived; refresh failure must not break the pipeline
        pass
    return StageResult(success=[], quarantined=[], skipped=[], bytes_in=0, bytes_out=0)
