"""WRITING stage — hash compare + atomic_commit per batch.

Target paths are computed under ``wiki.wiki_dir``. Per-doc OSError on
write moves the source to the wiki's poison_root and continues the batch.

Post-commit hook: when files are written, the stage also (a) registers
the collection with qmd (idempotent), (b) refreshes the qmd derived
index, and (c) regenerates ``wiki/index.md`` so the qmd-fallback
synthesizer stays in sync. The qmd hooks are non-fatal — a failure
must not roll back the wiki git commit that already landed.
"""

from __future__ import annotations

import hashlib
import sys
from typing import TYPE_CHECKING

from lies.collections.hash_manifest import HashManifest
from lies.collections.record import Collection
from lies.etl.quarantine import quarantine as move_to_poison
from lies.memory.index import rebuild_index
from lies.qmd.cli import qmd_collection_add_or_update, qmd_embed, qmd_update
from lies.wiki.git import atomic_commit
from lies.wiki.wiki import Wiki

if TYPE_CHECKING:
    from lies.etl.pipeline import StageResult


def run_write(
    wiki: Wiki,
    collection: Collection,
    normalized: list[tuple[str, str]],
    *,
    manifest: HashManifest,
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
        target = wiki.wiki_dir / path
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "w", encoding="utf-8") as f:
                f.write(markdown)
        except OSError as exc:
            quarantined.append((path, str(exc)))
            move_to_poison(wiki, collection.name, path, str(exc))
            continue
        manifest.update(path, sha)
        files.append(str(target.relative_to(wiki.data_root)))
        bytes_out += len(markdown.encode("utf-8"))
        success.append(path)

    if files:
        manifest.flush()
        atomic_commit(
            wiki.data_root,
            f"sync: {collection.name} +{len(success)} -{len(quarantined)} ~{len(skipped)}",
            files=files,
        )
        # Post-commit: make the new wiki state visible to qmd and the
        # qmd-fallback synthesizer. Failures are non-fatal — the wiki
        # commit already landed and is authoritative.
        try:
            qmd_collection_add_or_update(
                wiki.raw_dir, wiki.raw_dir / collection.name, collection.qmd_name()
            )
        except Exception as exc:  # noqa: BLE001 - qmd is derived; failures must not roll back the wiki commit
            print(
                f"warning: qmd collection registration failed for {collection.name!r}: {exc}; "
                f"continuing (wiki commit stands). Run `lies status` for state.",
                file=sys.stderr,
            )
        try:
            qmd_update(wiki.data_root)
        except Exception as exc:  # noqa: BLE001 - qmd is derived; failures must not roll back the wiki commit
            print(
                f"warning: qmd index update failed: {exc}; "
                f"continuing (wiki commit stands). Run `lies status` for state.",
                file=sys.stderr,
            )
        try:
            qmd_embed(wiki.data_root, collection.qmd_name())
        except Exception as exc:  # noqa: BLE001 - qmd is derived; failures must not roll back the wiki commit
            print(
                f"warning: qmd embed failed for {collection.name!r}: {exc}; "
                f"continuing (wiki commit stands). Run `lies status` (or `qmd status` directly if status also fails) for state.",
                file=sys.stderr,
            )
        try:
            rebuild_index(wiki)
        except Exception:  # noqa: BLE001, S110 - rebuild_index parses user-authored frontmatter; failures must not roll back the wiki commit
            pass

    return StageResult(
        success=success,
        quarantined=quarantined,
        skipped=skipped,
        bytes_in=0,
        bytes_out=bytes_out,
    )
