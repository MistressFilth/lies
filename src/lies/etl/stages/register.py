"""REGISTER stage — register a WikiCollectionRef on first successful sync.

Idempotent. Reads ``Collection.path`` and the wiki schema path; builds
a ``WikiCollectionRef`` and registers it on the in-memory
``WikiMemoryService``. Failure is non-fatal: the wiki commit already
happened, and the next sync re-attempts the registration.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from lies.memory.models import WikiCollectionRef
from lies.memory.service import WikiMemoryService

if TYPE_CHECKING:
    from lies.collections.record import Collection
    from lies.etl.pipeline import StageResult


def run_register(collection: Collection, service: WikiMemoryService) -> StageResult:
    from lies.etl.pipeline import StageResult

    if service.is_registered(collection.name):
        return StageResult(
            success=[],
            quarantined=[],
            skipped=[],
            parsed_docs=[],
        )
    ref = WikiCollectionRef(
        collection_id=collection.name,
        root=Path(collection.path).resolve().as_posix(),  # type: ignore[arg-type]
        qmd_collection=collection.qmd_name(),
        schema_path=(Path(collection.path).parent.parent / ".lies" / "schema.md")
        .resolve()
        .as_posix(),  # type: ignore[arg-type]
    )
    service.register_collection(ref)
    # No doc was written in this stage — registration is metadata only.
    # Telemetry consumers count ``len(success)`` for docs written; the
    # collection name would over-count the run by one.
    return StageResult(
        success=[],
        quarantined=[],
        skipped=[],
        parsed_docs=[],
    )
