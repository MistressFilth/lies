"""Read-only SQLite inspection of qmd's index.

qmd stores its corpus in a SQLite database at the cache root
(``$XDG_CACHE_HOME/qmd/index.sqlite``). The schema is:

- ``store_collections`` — registered collection metadata
- ``content`` — verbatim document bodies, keyed by content hash
- ``documents`` — one row per ``(collection, path)``, FK to ``content``
- ``content_vectors`` — embedding rows, one per chunk

The connection is opened read-only at every entry point. qmd has no
read-only mode, and a CLI call for diagnosis (e.g. ``qmd doctor``)
opened the live index read-write and wrote a row during the probe
that produced this module. The seam exists so that a read-only
inspection is the only inspection this module can perform.

A read-only connection is enforced by :func:`open_readonly`, the
module's only connection constructor. ``Test suite uses it too
(test_a_write_against_the_index_is_rejected), so a future change that
constructs a writable connection elsewhere fails the moment it is
touched.
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

    ``orphan_hashes`` counts distinct hash values; ``orphan_rows`` counts
    ``content_vectors`` rows. A single hash can hold multiple rows (one
    per chunk), so ``orphan_rows >= orphan_hashes`` always.

    Both counts are zero on a clean index.
    """

    orphan_hashes: int
    orphan_rows: int


def qmd_index_path() -> Path:
    """The path to qmd's index, resolved from ``$XDG_CACHE_HOME``.

    qmd's ``getDefaultDbPath`` resolves the default index to
    ``$XDG_CACHE_HOME/qmd/index.sqlite`` (or
    ``~/.cache/qmd/index.sqlite`` if XDG is unset) — confirmed in
    ``@tobilu/qmd/dist/store.js:418-433``. LIES reads the same path
    so ``lies qmd status`` inspects the index the daemon serves,
    not a sibling.
    """
    from lies.xdg import cache_home

    return cache_home() / "qmd" / "index.sqlite"


def open_readonly(db: Path) -> sqlite3.Connection:
    """Open the qmd index read-only. The only connection constructor.

    ``file:{db}?mode=ro`` + ``uri=True`` is what SQLite offers for
    read-only access; without ``mode=ro``, ``sqlite3.connect`` opens
    the file read-write and creates it if absent, which is exactly
    what the qmd probe did to the live index.

    Raises:
        sqlite3.OperationalError: ``db`` does not exist or is not a
            SQLite database.
    """
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def index_orphans(db: Path) -> OrphanReport:
    """Count ``content_vectors`` rows whose hash has no backing content.

    Two queries, one each for distinct hashes and rows. The row
    count is what ``qmd cleanup`` reports as ``Removed N orphaned
    embedding chunks``; the hash count is the distinct documents
    the cleanup affects.

    Live readings against ``$XDG_CACHE_HOME/qmd/index.sqlite`` over
    the 2026-10-03 session — 4 / 4 (probe-time, after the operator's
    unrelated sync re-created 4 single-row orphans), following the
    partner's ``qmd cleanup`` earlier that took 91070 → 48984
    vectors. The test that pins the contract uses a throwaway index
    with a known shape; live values are a reading, not an assertion.
    """
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

    A cheap, single-row check. The question it answers is "is this
    document retrievable through semantic search?" — yes iff an embedding
    has been persisted for the document's content hash.

    Empty / unknown hashes return False. The hash is bound as a query
    parameter, so no SQL is built from it.

    Returns ``False`` for an empty hash (no row can match), and for
    a hash that has no embedding rows. Designed as a guard:

    .. code-block:: python

        if not is_embedded(db, h):
            skip(h)
    """
    with closing(open_readonly(db)) as conn:
        cur = conn.execute(
            "SELECT 1 FROM content_vectors WHERE hash = ? LIMIT 1",
            (content_hash,),
        )
        return cur.fetchone() is not None


def collection_drift(db: Path) -> dict[str, list[str]]:
    """Per-collection drift conditions an operator should look at.

    Currently reports one condition: a registered path that no
    longer exists on disk. Empty-but-present collections (the
    ``wiki_default`` case, registered with an empty source tree) are
    **not** drift — they are a registered collection whose source
    has not been populated yet, and ``lies sync`` will index them
    on demand.

    Returns a ``{name: [messages]}`` map. An empty dict means
    no drift.

    A registered path that does not exist is not a soft hint; the
    ``qmd cleanup`` and ``qmd update`` verbs operate against the
    registered path, so a missing tree means qmd will silently
    produce nothing for that collection until the path is restored
    or the collection is removed with ``qmd collection remove``.
    """
    drift: dict[str, list[str]] = {}
    with closing(open_readonly(db)) as conn:
        rows = conn.execute("SELECT name, path FROM store_collections").fetchall()
    for name, path in rows:
        if not Path(path).exists():
            drift.setdefault(name, []).append(f"registered path does not exist on disk: {path}")
    return drift


def integrity_summary(db: Path) -> dict[str, Any]:
    """The full integrity snapshot for ``lies qmd status``.

    Combines the three named entry points (:func:`index_orphans`,
    :func:`is_embedded`'s coverage view, :func:`collection_drift`)
    with three coverage queries the plan's Step 6 names explicitly:

    - active-vs-total document split (``documents.active`` flag)
    - count of active documents lacking any embedding row
    - registered-collection count

    An empty-but-present registered collection (the ``wiki_default``
    case) is not in the drift map; it is registered with a real path
    and 0 documents, which is what ``lies sync`` populates.

    Returns a dict; the CLI command at ``src/lies/cli/qmd.py`` prints
    it under an ``index`` key alongside the daemon fields.
    """
    orphans = index_orphans(db)
    drift = collection_drift(db)
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
        "drift": drift,
    }
