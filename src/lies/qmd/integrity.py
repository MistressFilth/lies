"""Read-only SQLite inspection of qmd's index.

Corpus at ``$XDG_CACHE_HOME/qmd/index.sqlite``: ``store_collections``,
``content`` (bodies keyed by hash), ``documents`` (FK to ``content``),
``content_vectors`` (one row per chunk). Connections are read-only —
qmd has no read-only mode.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class OrphanReport:
    """Counts of ``content_vectors`` rows with no backing ``content``.
    Attributes:
        orphan_hashes: Distinct hashes with no row in ``content``.
        orphan_rows: ``content_vectors`` rows in that state. One hash
            spans chunks, so ``orphan_rows >= orphan_hashes``.
    """

    orphan_hashes: int
    orphan_rows: int


@dataclass(frozen=True)
class LiveIndexSnapshot:
    """Point-in-time aggregates for diffing two qmd-index readings.
    Attributes:
        collection_names: ``frozenset[str]`` of ``store_collections.name``.
        active_doc_count: ``COUNT(*) FROM documents WHERE active = 1``.
        total_vectors: ``COUNT(*) FROM content_vectors``.
        orphan_vectors: rows in ``content_vectors`` with no ``content``.
    """

    collection_names: frozenset[str]
    active_doc_count: int
    total_vectors: int
    orphan_vectors: int


def qmd_index_path() -> Path:
    """Path to qmd's index, resolved from ``$XDG_CACHE_HOME``.

    Matches qmd's ``getDefaultDbPath`` (``@tobilu/qmd/dist/store.js:418-433``).
    """
    from lies.xdg import cache_home

    return cache_home() / "qmd" / "index.sqlite"


def open_readonly(db: Path) -> sqlite3.Connection:
    """Open the qmd index read-only. The only connection constructor.

    ``file:{db}?mode=ro`` + ``uri=True``, with the path
    percent-escaped: a ``?`` or ``#`` in a path would otherwise end
    the path and start the query string, and the connection would
    open a different file than the caller named.

    A WAL database needing recovery is the case a caller meets
    before any of this is documented to them: a read-only
    connection cannot create the ``-shm`` file, so it gets
    ``SQLITE_CANTOPEN`` rather than a summary.

    Raises:
        sqlite3.OperationalError: ``db`` does not exist or is not a
            SQLite database.
    """
    from urllib.parse import quote

    return sqlite3.connect(f"file:{quote(str(db))}?mode=ro", uri=True)


def index_orphans(db: Path) -> OrphanReport:
    """Count ``content_vectors`` rows whose hash has no backing content."""
    with closing(open_readonly(db)) as conn:
        cur = conn.execute(
            "SELECT COUNT(*) FROM ("
            "SELECT DISTINCT hash FROM content_vectors "
            "WHERE hash NOT IN (SELECT hash FROM content)"
            ")"
        )
        orphan_hashes = cur.fetchone()[0]
        cur = conn.execute(
            "SELECT COUNT(*) FROM content_vectors WHERE hash NOT IN (SELECT hash FROM content)"
        )
        orphan_rows = cur.fetchone()[0]
    return OrphanReport(orphan_hashes=orphan_hashes, orphan_rows=orphan_rows)


def is_embedded(db: Path, content_hash: str) -> bool:
    """True iff at least one ``content_vectors`` row exists for ``content_hash``.

    Hash is bound as a query parameter; empty / unknown hashes return
    ``False``.
    """
    with closing(open_readonly(db)) as conn:
        cur = conn.execute(
            "SELECT 1 FROM content_vectors WHERE hash = ? LIMIT 1",
            (content_hash,),
        )
        return cur.fetchone() is not None


def collection_drift(db: Path) -> dict[str, list[str]]:
    """Per-collection drift an operator should look at.

    Reports registered paths that no longer exist on disk.
    Empty-but-present collections (``wiki_default``) are not drift.
    Returns ``{name: [messages]}``; empty means no drift.

    Note: a *collection name that has documents but no
    ``store_collections`` entry* is invisible to this function. The
    case is real (the live qmd index on this host had a
    ``wiki_tag-filter-lib`` residue with 5 documents and no
    registry entry that this function returned ``{}`` for). The
    companion :func:`document_drift` reports that case; the two
    are unioned in :func:`integrity_summary` so a drift never
    silently passes.
    """
    drift: dict[str, list[str]] = {}
    with closing(open_readonly(db)) as conn:
        rows = conn.execute("SELECT name, path FROM store_collections").fetchall()
    for name, path in rows:
        if not Path(path).exists():
            drift.setdefault(name, []).append(f"registered path does not exist on disk: {path}")
    return drift


def document_drift(db: Path) -> dict[str, list[str]]:
    """Documents whose ``collection`` field has no ``store_collections`` row.

    Reports ``{collection_name: [messages]}`` for every distinct
    ``documents.collection`` value that is absent from
    ``store_collections``. Empty means every document's collection
    is registered.

    The complement of :func:`collection_drift`, which iterates
    ``store_collections`` only. A collection that has documents but
    no registry entry is invisible to ``collection_drift`` while
    being a real drift — the documents were indexed against a
    collection the daemon no longer knows about, so any
    collection-filtered query will silently drop them. Measured
    on this host (2026-10-03): the residue was 5 documents under
    ``wiki_tag-filter-lib``; ``collection_drift`` returned ``{}``;
    this function returns the missed name with a single message.
    """
    drift: dict[str, list[str]] = {}
    with closing(open_readonly(db)) as conn:
        rows = conn.execute(
            "SELECT DISTINCT collection, COUNT(*) "
            "FROM documents "
            "WHERE collection NOT IN (SELECT name FROM store_collections) "
            "GROUP BY collection"
        ).fetchall()
    for collection_name, count in rows:
        drift.setdefault(collection_name, []).append(
            f"{count} document(s) reference a collection absent from store_collections"
        )
    return drift


def live_index_snapshot(db: Path) -> LiveIndexSnapshot | None:
    """Snapshot the four aggregates that detect a write to a qmd index.

    Returns ``None`` when ``db`` does not exist.
    """
    if not db.exists():
        return None
    with closing(open_readonly(db)) as conn:
        names = frozenset(row[0] for row in conn.execute("SELECT name FROM store_collections"))
        (active_doc_count,) = conn.execute(
            "SELECT COUNT(*) FROM documents WHERE active = 1"
        ).fetchone()
        (total_vectors,) = conn.execute("SELECT COUNT(*) FROM content_vectors").fetchone()
        (orphan_vectors,) = conn.execute(
            "SELECT COUNT(*) FROM content_vectors WHERE hash NOT IN (SELECT hash FROM content)"
        ).fetchone()
    return LiveIndexSnapshot(
        collection_names=names,
        active_doc_count=int(active_doc_count),
        total_vectors=int(total_vectors),
        orphan_vectors=int(orphan_vectors),
    )


def snapshots_differ(
    before: LiveIndexSnapshot | None,
    after: LiveIndexSnapshot | None,
) -> tuple[bool, str]:
    """``(changed, message)`` for the session guard's before/after pair.

    ``None`` on either side is a no-op. On a change, ``message``
    names the field and values on either side.
    """
    if before is None or after is None:
        return False, ""
    if before == after:
        return False, ""
    added = sorted(after.collection_names - before.collection_names)
    removed = sorted(before.collection_names - after.collection_names)
    msg = (
        "the live qmd index changed during this run: "
        f"collections before={sorted(before.collection_names)!r} "
        f"after={sorted(after.collection_names)!r} "
        f"(added={added!r}, removed={removed!r}); "
        f"active docs before={before.active_doc_count} "
        f"after={after.active_doc_count}; "
        f"total vectors before={before.total_vectors} "
        f"after={after.total_vectors}; "
        f"orphan vectors before={before.orphan_vectors} "
        f"after={after.orphan_vectors}. "
        f"This file's fixture is meant to write to a throwaway "
        f"under XDG_CACHE_HOME, not the live index."
    )
    return True, msg


def registry_divergence(db: Path) -> dict[str, Any] | None:
    """The registry is *empty* while documents still reference collections.

    Returns one finding describing the event, its counts, and its
    remedy; ``None`` when the registry is not empty.

    **Why this is not ordinary drift.** :func:`document_drift` reports
    each unregistered collection separately, so an emptied registry
    arrives as N near-identical entries that each name a collection the
    operator never lost. Measured on this host (2026-10-05): 117
    entries, every one reading "1 document(s) reference a collection
    absent from store_collections", for a corpus that was intact.

    **The cause, read from qmd 2.5.3's source.** ``syncConfigToDb``
    (``dist/store.js:887``) upserts the external config's collections
    and then deletes every ``store_collections`` row the config does
    not name. It early-returns when ``store_config.config_hash``
    already matches. A config that momentarily declares zero
    collections therefore empties the table, and the hash written for
    *that* config then matches — making the wipe self-perpetuating
    until the config changes again. The config is
    ``~/.config/qmd/index.yml``.

    **Why retrieval kept working.** The daemon serves reads without
    passing through that sync, and ``validate_scope`` reads the
    daemon's own ``status`` tool rather than ``store_collections``. So
    every query path saw a healthy index while ``lies qmd status``
    reported 117 broken collections.

    Deliberately narrow: it fires only on a *fully* empty registry.
    A partial overlap is ordinary per-collection drift with a
    different remedy, and escalating it here would bury the precise
    ``document_drift`` finding under a diagnosis that does not fit.
    """
    with closing(open_readonly(db)) as conn:
        registered = conn.execute("SELECT COUNT(*) FROM store_collections").fetchone()[0]
        referenced = conn.execute("SELECT COUNT(DISTINCT collection) FROM documents").fetchone()[0]
        documents = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]

    if registered or not referenced:
        return None

    return {
        "registered": registered,
        "referenced": referenced,
        "documents": documents,
        "cause": (
            "qmd's syncConfigToDb (dist/store.js:887) deletes every "
            "store_collections row the external config does not name, and "
            "early-returns while store_config.config_hash matches. A config "
            "declaring zero collections emptied the table; the hash written "
            "for it then matched, so the wipe did not self-repair."
        ),
        "config": "~/.config/qmd/index.yml",
        "remedy": (
            "Check the collections block in ~/.config/qmd/index.yml. If it "
            "still declares them, any qmd command re-syncs the config into "
            "store_collections and the divergence clears. If it does not, "
            "restore the config first -- re-syncing an empty config is what "
            "emptied the table."
        ),
    }


def integrity_summary(db: Path) -> dict[str, Any]:
    """Full integrity snapshot for ``lies qmd status``.

    Combines :func:`index_orphans`, :func:`collection_drift`, and
    :func:`document_drift` with three coverage queries. The two
    drift functions are complementary: ``collection_drift`` walks
    ``store_collections`` and finds registered paths missing on
    disk; ``document_drift`` walks ``documents`` and finds
    unregistered collections. A drift that exists on one side
    only is invisible to the other; the union closes the class.

    ``registry_divergence`` *supplements* that union rather than
    replacing any of it. It names the one case where the per-collection
    entries are symptoms of a single event, and carries the cause and the
    remedy; the raw ``document_drift`` breakdown stays so nothing is lost
    and a consumer parsing the summary cannot mistake a divergence for a
    healthy index.
    """
    orphans = index_orphans(db)
    collection_d = collection_drift(db)
    document_d = document_drift(db)
    with closing(open_readonly(db)) as conn:
        cur = conn.execute("SELECT COUNT(*) FROM documents")
        documents_total = cur.fetchone()[0]
        cur = conn.execute("SELECT COUNT(*) FROM documents WHERE active = 1")
        documents_active = cur.fetchone()[0]
        cur = conn.execute(
            "SELECT COUNT(*) FROM documents "
            "WHERE active = 1 "
            "AND hash NOT IN (SELECT hash FROM content_vectors)"
        )
        documents_active_without_vectors = cur.fetchone()[0]
        cur = conn.execute("SELECT COUNT(*) FROM store_collections")
        collections = cur.fetchone()[0]
    return {
        "path": str(db),
        "orphan_hashes": orphans.orphan_hashes,
        "orphan_rows": orphans.orphan_rows,
        "documents_total": documents_total,
        "documents_active": documents_active,
        "documents_active_without_vectors": documents_active_without_vectors,
        "collections": collections,
        "drift": collection_d,
        "document_drift": document_d,
        "registry_divergence": registry_divergence(db),
    }
