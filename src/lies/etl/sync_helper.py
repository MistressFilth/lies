"""Extracted sync orchestration for CLI + library use.

The Typer CLI subcommands delegate here. Tests target this helper
directly via ``runner.invoke(app, [...])``; the helper is the single
place where orchestration lives.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from lies.collections.hash_manifest import HashManifest
from lies.collections.record import Collection, load_collection
from lies.etl.cost import CostBudget
from lies.etl.heartbeat import (
    Heartbeat,
    clear_heartbeat,
    heartbeat_is_stale,
    read_heartbeat,
    wait_until_free,
    write_heartbeat,
)
from lies.etl.pipeline import SyncOrchestrator
from lies.etl.telemetry import SyncTelemetry


def acquire_heartbeat(wiki_root: Path, *, wait: bool, fail_busy: bool) -> Heartbeat | None:
    """Returns the heartbeat if one was successfully claimed; None if busy.

    Caller is responsible for invoking ``release_heartbeat`` afterwards.
    """
    hb = read_heartbeat(wiki_root)
    if hb and not heartbeat_is_stale(hb):
        if fail_busy or not wait:
            return None
        wait_until_free(wiki_root)
    h = Heartbeat(pid=os.getpid(), started_at=time.time(), collection="*")
    write_heartbeat(wiki_root, h)
    return h


def release_heartbeat(wiki_root: Path) -> None:
    clear_heartbeat(wiki_root)


def collection_names(wiki_root: Path, only: str | None) -> list[str]:
    if only:
        return [only]
    cfg_dir = wiki_root / ".lies" / "collections"
    return sorted(p.stem for p in cfg_dir.glob("*.yaml"))


def sync_collection(
    wiki_root: Path,
    name: str,
    *,
    force: bool = False,
) -> None:
    """Run SyncOrchestrator for a single collection."""
    collection: Collection = load_collection(wiki_root, name)
    telemetry = SyncTelemetry(name, wiki_root / ".lies" / "logs")
    budget = CostBudget()
    manifest = HashManifest(wiki_root, name)
    pipeline = SyncOrchestrator(
        collection=collection,
        telemetry=telemetry,
        budget=budget,
        manifest=manifest,
        wiki_root=wiki_root,
        force=force,
    )
    pipeline.run()
    telemetry.close()
