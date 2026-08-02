"""WRITING stage — hash compare + atomic_commit per batch.

Target paths are computed under ``<wiki_root>/wiki/<path>`` (NOT
CWD-relative). Per-doc OSError on write moves the source to
``.lies/poison/<collection>/<path>`` and continues the batch.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

from lies.collections.hash_manifest import HashManifest
from lies.collections.record import Collection
from lies.etl.quarantine import quarantine as move_to_poison
from lies.wiki.git import atomic_commit

if TYPE_CHECKING:
    from lies.etl.pipeline import StageResult


def run_write(
    collection: Collection,
    normalized: list[tuple[str, str]],
    *,
    manifest: HashManifest,
    wiki_root: Path,
    force: bool = False,
) -> StageResult:
    from lies.etl.pipeline import StageResult

    success: list[str] = []
    skipped: list[str] = []
    quarantined: list[tuple[str, str]] = []
    files: list[str] = []
    bytes_out = 0

    for path, markdown in normalized:
        sha = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
        if not force and manifest.compare(path, sha):
            skipped.append(path)
            continue
        target = wiki_root / "wiki" / path
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "w", encoding="utf-8") as f:
                f.write(markdown)
        except OSError as exc:
            quarantined.append((path, str(exc)))
            move_to_poison(wiki_root, collection.name, path, str(exc))
            continue
        manifest.update(path, sha)
        files.append(str(target.relative_to(wiki_root)))
        bytes_out += len(markdown.encode("utf-8"))
        success.append(path)

    if files:
        manifest.flush()
        atomic_commit(
            wiki_root,
            f"sync: {collection.name} +{len(success)} -{len(quarantined)} ~{len(skipped)}",
            files=files,
        )

    return StageResult(
        success=success, quarantined=quarantined, skipped=skipped,
        bytes_in=0, bytes_out=bytes_out,
    )
