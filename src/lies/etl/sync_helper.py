"""Extracted sync orchestration for CLI + library use.

The Typer CLI subcommands delegate here. Tests target this helper
directly via ``runner.invoke(app, [...])``; the helper is the single
place where orchestration lives.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from pathlib import Path

from lies.collections.record import Collection, load_collection
from lies.etl.heartbeat import (
    MAX_SYNC_AGE_S,
    Heartbeat,
    clear_heartbeat,
    heartbeat_is_stale,
    read_heartbeat,
    release_create_lock,
    wait_until_free,
    write_heartbeat,
)
from lies.library import ingest as _library_ingest
from lies.library.fetcher import ScraperFetcher
from lies.library.ingest import BatchIngestResult
from lies.library.paths import Library
from lies.lock_errors import WikiFlockIndeterminate
from lies.utils.exclusive import acquire_create_lock
from lies.wiki.wiki import Wiki


def acquire_heartbeat(wiki: Wiki, *, wait: bool, fail_busy: bool) -> Heartbeat | None:
    """Returns the heartbeat if one was successfully claimed; None if busy.

    Concurrency: takes an atomic ``O_CREAT | O_EXCL`` create on
    ``$XDG_RUNTIME_DIR/lies/<wiki>/sync.lock.create`` *before*
    reading or writing the heartbeat file. Two processes racing on
    ``acquire_heartbeat`` cannot both succeed; the loser observes
    ``None`` (or waits, if ``wait=True``).

    When the contender's liveness cannot be determined (``os.kill`` EPERM
    or unknown OSError) AND the heartbeat is older than the recovery
    window, the contender is reported as :class:`WikiFlockIndeterminate`
    — operator must run ``lies flock <name> force-repair`` to inspect or
    kill the holder manually. The primitive does not reap.

    Caller is responsible for invoking :func:`release_heartbeat`
    afterwards, which closes the create-lock fd and unlinks the lock
    file as well as clearing the heartbeat.
    """
    result = acquire_create_lock(wiki.sync_create_lock_path, max_age_s=MAX_SYNC_AGE_S)
    if result is None:
        # Another process already holds the create lock.
        hb = read_heartbeat(wiki)
        if hb and not heartbeat_is_stale(hb):
            if fail_busy or not wait:
                return None
            wait_until_free(wiki)
            # Retry the create after the holder releases.
            result = acquire_create_lock(wiki.sync_create_lock_path, max_age_s=MAX_SYNC_AGE_S)
            if result is None:
                return None
        else:
            # Stale or absent heartbeat but the create lock is held by
            # a concurrent acquirer; treat as busy.
            if fail_busy or not wait:
                return None
            wait_until_free(wiki)
            result = acquire_create_lock(wiki.sync_create_lock_path, max_age_s=MAX_SYNC_AGE_S)
            if result is None:
                return None
    if result.status == "indeterminate":
        t = result.holder_started_at
        started_at = datetime.fromtimestamp(t, tz=UTC).isoformat() if t is not None else "unknown"
        raise WikiFlockIndeterminate(
            f"{wiki.name} flock held by an indeterminate process "
            f"(pid {result.holder_pid}, started {started_at}); "
            f"cannot determine live state. "
            f"Run `lies flock {wiki.name} force-repair` to inspect/retry "
            f"or kill {result.holder_pid} manually."
        )
    fd = result.fd
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
    collection_name: str,
    *,
    force: bool = False,
) -> BatchIngestResult:
    """Sync a single collection into the library.

    Wiki is kept as the first parameter for ``--wait`` / ``--fail-busy``
    semantics and ``--name`` qmd-side resolution (the flock lives on the
    wiki's ``data_root``), but the WRITE TARGET is the library singleton
    at ``xdg.data_home() / LIES_DATA_SUBDIR / library``. The collection's
    registered source (``Collection.source``) is resolved from the wiki's
    ``collections_dir/<name>.yaml`` and handed to ``run_batch_ingest``
    along with the library instance and a ``ScraperFetcher``.

    Returns the :class:`BatchIngestResult` from ``run_batch_ingest`` so
    the CLI can surface non-zero ``result.errors`` to the operator
    instead of silently exiting 0 on a wholly-failed batch.

    ``Collection.scraper_cmd`` is honored: when set, the
    ``ScraperFetcher`` loads the bespoke scraper instead of falling
    through to ``pick_scraper``. The :class:`Collection` itself is
    threaded through so REGISTRY builders (sphinx, liquid, bespoke)
    that read ``collection.config`` keep working.
    """
    collection: Collection = load_collection(wiki, collection_name)
    # ``Collection.source`` is a string — either a URL (``http(s)://`` /
    # ``git@``) or a local filesystem path. ``Path(...)`` mangles
    # ``https://`` to ``https:/`` on POSIX, which breaks the
    # ``pick_scraper`` URL prefix check; keep the URL as a string and
    # wrap local paths in ``Path`` for the ``source_dir`` parameter.
    source = collection.source
    if source.startswith(("http://", "https://", "git@")):
        source_dir: Path | str = source
    else:
        source_dir = Path(source)
    library = Library.open()
    fetcher = ScraperFetcher(
        library=library,
        scraper_cmd=collection.scraper_cmd,
        collection=collection,
    )
    return _library_ingest.run_batch_ingest(
        library=library,
        collection_name=collection_name,
        source_dir=source_dir,
        fetcher=fetcher,
        force=force,
    )
