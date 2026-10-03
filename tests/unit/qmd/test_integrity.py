"""Unit tests for ``lies.qmd.integrity`` — read-only qmd index inspection.

The module opens the qmd index read-only (``file:...?mode=ro``) at every
entry point, because qmd has no read-only open mode. The connection
helper :func:`open_readonly` is the only constructor — every test that
exercises a read-only connection goes through it, so a future change
that builds a writable connection elsewhere fails the moment the
``test_a_write_against_the_index_is_rejected`` shape is touched.

The schema in :func:`_tiny_index` mirrors the live schema
(``store_collections``, ``content``, ``documents``,
``content_vectors``) read off ``$XDG_CACHE_HOME/qmd/index.sqlite`` on
2026-10-03 via ``.schema`` over a read-only connection. No triggers,
no FTS5, no ``documents_fts`` — those are qmd internals and the
integrity surface does not query them.

Live values are not asserted against. The test fixtures build a known
shape; the live index is a measurement, not a fixture.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

import pytest

from lies.qmd.integrity import (
    OrphanReport,
    collection_drift,
    index_orphans,
    integrity_summary,
    is_embedded,
    open_readonly,
    qmd_index_path,
)


_SCHEMA = """
CREATE TABLE store_collections (
    name TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    pattern TEXT NOT NULL DEFAULT '**/*.md',
    ignore_patterns TEXT,
    include_by_default INTEGER DEFAULT 1,
    update_command TEXT,
    context TEXT
);
CREATE TABLE content (
    hash TEXT PRIMARY KEY,
    doc TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collection TEXT NOT NULL,
    path TEXT NOT NULL,
    title TEXT NOT NULL,
    hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    modified_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY (hash) REFERENCES content(hash) ON DELETE CASCADE,
    UNIQUE(collection, path)
);
CREATE TABLE content_vectors (
    hash TEXT NOT NULL,
    seq INTEGER NOT NULL DEFAULT 0,
    pos INTEGER NOT NULL DEFAULT 0,
    model TEXT NOT NULL,
    embed_fingerprint TEXT NOT NULL DEFAULT '',
    total_chunks INTEGER NOT NULL DEFAULT 1,
    embedded_at TEXT NOT NULL,
    PRIMARY KEY (hash, seq)
);
"""


@pytest.fixture(scope="session")
def _empty_index_template() -> Path:
    """A session-scoped empty sqlite3 file with the schema pre-built.

    Schema construction on cold cache costs ~350ms per call on this
    host (ext4 /tmp). A session-scoped fixture builds the template
    once, and every test that calls :func:`_tiny_index` copies it
    rather than rebuilding. The copy is per-test, so test data does
    not leak; the schema build is not.
    """
    fd, name = tempfile.mkstemp(suffix=".sqlite", prefix="lies-empty-index-")
    import os

    os.close(fd)
    template = Path(name)
    with sqlite3.connect(str(template)) as conn:
        conn.executescript(_SCHEMA)
    return template


def _tiny_index(tmp_path: Path, template: Path) -> Path:
    """Copy the session template into the test's tmp_path.

    Each test gets its own file, so inserts do not leak across tests.
    The cost is one ``shutil.copyfile`` (a few ms on this host) instead
    of one ``executescript`` (~350ms on cold cache).
    """
    db = tmp_path / "index.sqlite"
    shutil.copyfile(template, db)
    return db


def test_open_readonly_returns_a_connection(tmp_path: Path, _empty_index_template: Path) -> None:
    db = _tiny_index(tmp_path, _empty_index_template)
    conn = open_readonly(db)
    try:
        # The pragma that proves it is a real, opened database.
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        names = {row[0] for row in cur.fetchall()}
        assert "content" in names
        assert "documents" in names
        assert "content_vectors" in names
        assert "store_collections" in names
    finally:
        conn.close()


def test_open_readonly_raises_when_the_index_does_not_exist(
    tmp_path: Path, _empty_index_template: Path
) -> None:
    missing = tmp_path / "does-not-exist.sqlite"
    with pytest.raises(sqlite3.OperationalError):
        open_readonly(missing)


def test_a_write_against_the_index_is_rejected(tmp_path: Path, _empty_index_template: Path) -> None:
    """The connection helper must open the database in mode=ro.

    The shape — ``with pytest.raises(sqlite3.OperationalError):`` —
    is from plan Step 4. It exercises :func:`open_readonly` itself,
    not a locally-built connection, so a future change that swaps
    the helper for a writable constructor fails the moment the
    shape is touched.
    """
    db = _tiny_index(tmp_path, _empty_index_template)
    with pytest.raises(sqlite3.OperationalError):
        with closing(open_readonly(db)) as conn:
            conn.execute("DELETE FROM content")


def test_orphans_are_content_vectors_rows_with_no_backing_content(
    tmp_path: Path, _empty_index_template: Path
) -> None:
    """Plan Step 1 — one orphan hash, two rows.

    The shape matches the live schema: ``content_vectors`` is keyed
    by ``(hash, seq)`` so one hash can carry multiple chunk rows.
    """
    db = _tiny_index(tmp_path, _empty_index_template)
    with sqlite3.connect(str(db)) as conn:
        # One backing document: its content row exists, its vectors
        # all match.
        conn.execute(
            "INSERT INTO content(hash, doc, created_at) VALUES (?, ?, ?)",
            ("goodhash", "good doc", "2026-10-03T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO documents(collection, path, title, hash, "
            "created_at, modified_at, active) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("c", "good.md", "Good", "goodhash", "2026-10-03T00:00:00Z", "2026-10-03T00:00:00Z", 1),
        )
        conn.execute(
            "INSERT INTO content_vectors(hash, seq, pos, model, "
            "embed_fingerprint, total_chunks, embedded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("goodhash", 0, 0, "m", "fp", 1, "2026-10-03T00:00:00Z"),
        )
        # One orphan hash: two vector rows, no content row, no
        # document. ``content_vectors`` has no FK, so SQLite
        # accepts this — and that's the defect.
        conn.execute(
            "INSERT INTO content_vectors(hash, seq, pos, model, "
            "embed_fingerprint, total_chunks, embedded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("orphan", 0, 0, "m", "fp", 2, "2026-10-03T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO content_vectors(hash, seq, pos, model, "
            "embed_fingerprint, total_chunks, embedded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("orphan", 1, 0, "m", "fp", 2, "2026-10-03T00:00:00Z"),
        )
        conn.commit()

    report = index_orphans(db)
    assert report.orphan_hashes == 1
    assert report.orphan_rows == 2


def test_a_clean_index_reports_zero_orphans(tmp_path: Path, _empty_index_template: Path) -> None:
    """Every content_vectors row has a backing content row.

    Pins the contract that ``qmd cleanup`` leaves the index in: 0/0.
    """
    db = _tiny_index(tmp_path, _empty_index_template)
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "INSERT INTO content(hash, doc, created_at) VALUES (?, ?, ?)",
            ("h", "d", "2026-10-03T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO content_vectors(hash, seq, pos, model, "
            "embed_fingerprint, total_chunks, embedded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("h", 0, 0, "m", "fp", 1, "2026-10-03T00:00:00Z"),
        )
        conn.commit()
    assert index_orphans(db) == OrphanReport(orphan_hashes=0, orphan_rows=0)


def test_is_embedded_returns_true_when_a_vector_row_exists(
    tmp_path: Path, _empty_index_template: Path
) -> None:
    db = _tiny_index(tmp_path, _empty_index_template)
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "INSERT INTO content_vectors(hash, seq, pos, model, "
            "embed_fingerprint, total_chunks, embedded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("abcdef", 0, 0, "m", "fp", 1, "2026-10-03T00:00:00Z"),
        )
        conn.commit()
    assert is_embedded(db, "abcdef") is True


def test_is_embedded_returns_false_for_an_unknown_hash(
    tmp_path: Path, _empty_index_template: Path
) -> None:
    db = _tiny_index(tmp_path, _empty_index_template)
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "INSERT INTO content_vectors(hash, seq, pos, model, "
            "embed_fingerprint, total_chunks, embedded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("abcdef", 0, 0, "m", "fp", 1, "2026-10-03T00:00:00Z"),
        )
        conn.commit()
    assert is_embedded(db, "not-in-the-index") is False


def test_is_embedded_returns_false_for_an_empty_hash(
    tmp_path: Path, _empty_index_template: Path
) -> None:
    """An empty hash cannot match any row, by definition.

    No row in ``content_vectors`` carries an empty ``hash`` (the
    column is ``NOT NULL``), so the parametrised query returns
    ``fetchone() is None``. The bool return is honest.
    """
    db = _tiny_index(tmp_path, _empty_index_template)
    assert is_embedded(db, "") is False


def test_is_embedded_is_a_single_row_check(tmp_path: Path, _empty_index_template: Path) -> None:
    """One row is enough — the count of rows is not the question.

    The function answers "is there at least one row?" not "how
    many?". Three rows for one hash still return ``True``.
    """
    db = _tiny_index(tmp_path, _empty_index_template)
    with sqlite3.connect(str(db)) as conn:
        for seq in range(3):
            conn.execute(
                "INSERT INTO content_vectors(hash, seq, pos, model, "
                "embed_fingerprint, total_chunks, embedded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("chunked", seq, 0, "m", "fp", 3, "2026-10-03T00:00:00Z"),
            )
        conn.commit()
    assert is_embedded(db, "chunked") is True


def test_collection_drift_reports_missing_paths(
    tmp_path: Path, _empty_index_template: Path
) -> None:
    """A registered path that does not exist on disk is drift.

    Mirrors the live observation on 2026-10-03: ``wiki_tag-filter-lib``
    is registered, holds 5 documents, but its path
    ``/home/.../collections/wiki_tag-filter-lib`` no longer exists.
    The function surfaces that as drift so the operator can either
    restore the path or ``qmd collection remove`` the entry.
    """
    db = _tiny_index(tmp_path, _empty_index_template)
    present = tmp_path / "present"  # created below
    present.mkdir()
    missing = tmp_path / "missing"  # never created
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "INSERT INTO store_collections(name, path) VALUES (?, ?)",
            ("present", str(present)),
        )
        conn.execute(
            "INSERT INTO store_collections(name, path) VALUES (?, ?)",
            ("missing", str(missing)),
        )
        conn.commit()

    drift = collection_drift(db)
    assert "missing" in drift
    assert "present" not in drift
    assert any("does not exist" in m for m in drift["missing"])


def test_collection_drift_is_empty_for_an_index_with_no_drift(
    tmp_path: Path, _empty_index_template: Path
) -> None:
    """Every registered path exists on disk ⇒ empty dict."""
    db = _tiny_index(tmp_path, _empty_index_template)
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "INSERT INTO store_collections(name, path) VALUES (?, ?)",
            ("a", str(a)),
        )
        conn.execute(
            "INSERT INTO store_collections(name, path) VALUES (?, ?)",
            ("b", str(b)),
        )
        conn.commit()
    assert collection_drift(db) == {}


def test_collection_drift_does_not_treat_empty_collections_as_drift(
    tmp_path: Path, _empty_index_template: Path
) -> None:
    """An empty registered path is the ``wiki_default`` case, not drift.

    The live ``wiki_default`` collection registers an empty directory
    (``~/.local/share/lies/default/wiki``) with 0 files; that is the
    qmd default wiki registration, expected, and ``lies sync`` will
    populate it on demand. The function must not report it.
    """
    db = _tiny_index(tmp_path, _empty_index_template)
    empty = tmp_path / "empty_wiki"
    empty.mkdir()  # exists, but contains no files
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "INSERT INTO store_collections(name, path) VALUES (?, ?)",
            ("wiki_default", str(empty)),
        )
        conn.commit()
    assert collection_drift(db) == {}


def test_collection_drift_reports_multiple_messages_per_collection(
    tmp_path: Path, _empty_index_template: Path
) -> None:
    """Future messages append rather than overwrite.

    Today there is only one drift condition (missing path), so the
    list always has length 0 or 1. The shape ``dict[str, list[str]]``
    leaves room for more messages without a breaking change. This
    test pins that today, against a future change that adds a second
    condition and a hand-edit that collapses the list to a string.
    """
    db = _tiny_index(tmp_path, _empty_index_template)
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "INSERT INTO store_collections(name, path) VALUES (?, ?)",
            ("x", "/this/path/is/not/here"),
        )
        conn.commit()
    drift = collection_drift(db)
    assert "x" in drift
    assert isinstance(drift["x"], list)
    assert len(drift["x"]) >= 1


def test_qmd_index_path_resolves_under_xdg_cache_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _empty_index_template: Path
) -> None:
    """The default index path is ``$XDG_CACHE_HOME/qmd/index.sqlite``.

    Mirrors qmd's own ``getDefaultDbPath`` (verified in
    ``@tobilu/qmd/dist/store.js:418-433``). LIES reads the same
    path so ``lies qmd status`` inspects the index the daemon
    serves, not a sibling.
    """
    cache = tmp_path / "cache"
    monkeypatch.setenv("LIES_XDG_CACHE_HOME", str(cache))
    assert qmd_index_path() == cache / "qmd" / "index.sqlite"


def test_integrity_summary_returns_a_complete_snapshot(
    tmp_path: Path, _empty_index_template: Path
) -> None:
    """``integrity_summary`` is the JSON shape ``lies qmd status`` prints.

    The fields the plan's Step 6 names are all present:

    - ``orphan_hashes`` and ``orphan_rows``
    - ``documents_total`` and ``documents_active`` (the active-vs-total split)
    - ``documents_active_without_vectors`` (every active doc has vectors?)
    - ``collections``
    - ``drift``
    """
    db = _tiny_index(tmp_path, _empty_index_template)
    a = tmp_path / "a"
    a.mkdir()
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "INSERT INTO content(hash, doc, created_at) VALUES (?, ?, ?)",
            ("h", "d", "2026-10-03T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO documents(collection, path, title, hash, "
            "created_at, modified_at, active) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("c", "p.md", "t", "h", "2026-10-03T00:00:00Z", "2026-10-03T00:00:00Z", 1),
        )
        conn.execute(
            "INSERT INTO content_vectors(hash, seq, pos, model, "
            "embed_fingerprint, total_chunks, embedded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("h", 0, 0, "m", "fp", 1, "2026-10-03T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO store_collections(name, path) VALUES (?, ?)",
            ("a", str(a)),
        )
        conn.commit()

    summary = integrity_summary(db)
    assert summary["path"] == str(db)
    assert summary["orphan_hashes"] == 0
    assert summary["orphan_rows"] == 0
    assert summary["documents_total"] == 1
    assert summary["documents_active"] == 1
    assert summary["documents_active_without_vectors"] == 0
    assert summary["collections"] == 1
    assert summary["drift"] == {}


def test_integrity_summary_reports_active_docs_that_lack_vectors(
    tmp_path: Path, _empty_index_template: Path
) -> None:
    """An active document with no ``content_vectors`` row is a coverage gap.

    The plan's Step 6 says "whether every active document has
    vectors". The boolean answer is encoded as the count of
    documents that don't — ``0`` means every active doc has
    vectors; anything else is a gap the operator can act on.
    """
    db = _tiny_index(tmp_path, _empty_index_template)
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "INSERT INTO content(hash, doc, created_at) VALUES (?, ?, ?)",
            ("h_yes", "doc", "2026-10-03T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO content(hash, doc, created_at) VALUES (?, ?, ?)",
            ("h_no", "doc", "2026-10-03T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO documents(collection, path, title, hash, "
            "created_at, modified_at, active) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("c", "yes.md", "Yes", "h_yes", "2026-10-03T00:00:00Z", "2026-10-03T00:00:00Z", 1),
        )
        conn.execute(
            "INSERT INTO documents(collection, path, title, hash, "
            "created_at, modified_at, active) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("c", "no.md", "No", "h_no", "2026-10-03T00:00:00Z", "2026-10-03T00:00:00Z", 1),
        )
        conn.execute(
            "INSERT INTO content_vectors(hash, seq, pos, model, "
            "embed_fingerprint, total_chunks, embedded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("h_yes", 0, 0, "m", "fp", 1, "2026-10-03T00:00:00Z"),
        )
        conn.commit()

    summary = integrity_summary(db)
    assert summary["documents_total"] == 2
    assert summary["documents_active"] == 2
    assert summary["documents_active_without_vectors"] == 1


def test_integrity_summary_reports_inactive_documents_separately(
    tmp_path: Path, _empty_index_template: Path
) -> None:
    """The active-vs-total split surfaces ``active=0`` rows.

    qmd's ``update`` marks documents inactive before removing them;
    an inactive row in the live index is normal during a sync but a
    leak if it stays past one. The summary surfaces the split so a
    reader can see both numbers.
    """
    db = _tiny_index(tmp_path, _empty_index_template)
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "INSERT INTO documents(collection, path, title, hash, "
            "created_at, modified_at, active) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("c", "live.md", "T", "h", "2026-10-03T00:00:00Z", "2026-10-03T00:00:00Z", 1),
        )
        conn.execute(
            "INSERT INTO documents(collection, path, title, hash, "
            "created_at, modified_at, active) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("c", "gone.md", "T", "h", "2026-10-03T00:00:00Z", "2026-10-03T00:00:00Z", 0),
        )
        conn.commit()
    summary = integrity_summary(db)
    assert summary["documents_total"] == 2
    assert summary["documents_active"] == 1


def test_integrity_summary_surfaces_drift(tmp_path: Path, _empty_index_template: Path) -> None:
    """A registered path missing on disk appears under ``drift``."""
    db = _tiny_index(tmp_path, _empty_index_template)
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "INSERT INTO store_collections(name, path) VALUES (?, ?)",
            ("stale", "/this/path/is/gone"),
        )
        conn.commit()
    summary = integrity_summary(db)
    assert "stale" in summary["drift"]
