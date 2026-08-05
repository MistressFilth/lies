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
    acquire_create_lock,
    clear_heartbeat,
    heartbeat_is_stale,
    read_heartbeat,
    release_create_lock,
    wait_until_free,
    write_heartbeat,
)
from lies.etl.pipeline import SyncOrchestrator
from lies.etl.telemetry import SyncTelemetry
from lies.wiki.wiki import Wiki


def acquire_heartbeat(wiki: Wiki, *, wait: bool, fail_busy: bool) -> Heartbeat | None:
    """Returns the heartbeat if one was successfully claimed; None if busy.

    Concurrency: takes an atomic ``O_CREAT | O_EXCL`` create on
    ``$XDG_RUNTIME_DIR/lies/<wiki>/sync.lock.create`` *before*
    reading or writing the heartbeat file. Two processes racing on
    ``acquire_heartbeat`` cannot both succeed; the loser observes
    ``None`` (or waits, if ``wait=True``).

    Caller is responsible for invoking :func:`release_heartbeat`
    afterwards, which closes the create-lock fd and unlinks the lock
    file as well as clearing the heartbeat.
    """
    fd = acquire_create_lock(wiki)
    if fd is None:
        # Another process already holds the create lock.
        hb = read_heartbeat(wiki)
        if hb and not heartbeat_is_stale(hb):
            if fail_busy or not wait:
                return None
            wait_until_free(wiki)
            # Retry the create after the holder releases.
            fd = acquire_create_lock(wiki)
            if fd is None:
                return None
        else:
            # Stale or absent heartbeat but the create lock is held by
            # a concurrent acquirer; treat as busy.
            if fail_busy or not wait:
                return None
            wait_until_free(wiki)
            fd = acquire_create_lock(wiki)
            if fd is None:
                return None
    # We hold the create lock; the heartbeat file is now safe to write.
    hb = read_heartbeat(wiki)
    if hb and not heartbeat_is_stale(hb):
        # Lost a race after the wait released; close the fd and bail.
        release_create_lock(wiki, fd)
        return None
    h = Heartbeat(pid=os.getpid(), started_at=time.time(), collection="*")
    write_heartbeat(wiki, h)
    # Persist the fd so release_heartbeat can close + unlink.
    _heartbeat_fd_path(wiki).write_text(str(fd), encoding="utf-8")
    return h


def _heartbeat_fd_path(wiki: Wiki) -> Path:
    """Sidecar file holding the create-lock fd for the active heartbeat."""
    return wiki.sync_fd_path


def release_heartbeat(wiki: Wiki) -> None:
    """Clear the heartbeat and release the atomic create lock."""
    clear_heartbeat(wiki)
    fd_path = _heartbeat_fd_path(wiki)
    fd: int | None = None
    if fd_path.exists():
        try:
            fd = int(fd_path.read_text(encoding="utf-8").strip() or "0") or None
        except ValueError:
            fd = None
        try:
            fd_path.unlink()
        except FileNotFoundError:
            pass
    release_create_lock(wiki, fd)


def collection_names(wiki: Wiki, only: str | None) -> list[str]:
    if only:
        return [only]
    cfg_dir = wiki.collections_dir
    return sorted(p.stem for p in cfg_dir.glob("*.yaml"))


def sync_collection(
    wiki: Wiki,
    name: str,
    *,
    force: bool = False,
) -> None:
    """Run SyncOrchestrator for a single collection.

    Errors if the collection YAML is missing from
    ``wiki.collections_dir``; use ``sync_helper.bootstrap_collection``
    (or write the YAML by hand) before calling. The first-time LLM
    scraper generation flow that was promised in the original docstring
    is deferred.
    """
    collection: Collection = load_collection(wiki.data_root, name)
    with SyncTelemetry(wiki, name) as telemetry:
        budget = CostBudget()
        manifest = HashManifest(wiki.data_root, name)
        pipeline = SyncOrchestrator(
            collection=collection,
            telemetry=telemetry,
            budget=budget,
            manifest=manifest,
            wiki_root=wiki.data_root,
            force=force,
        )
        pipeline.run()
