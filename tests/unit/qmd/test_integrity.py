"""Unit tests for ``lies.qmd.integrity`` — read-only qmd index inspection."""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

import pytest

from lies.qmd.integrity import (
    LiveIndexSnapshot,
    OrphanReport,
    collection_drift,
    index_orphans,
    integrity_summary,
    is_embedded,
    live_index_snapshot,
    open_readonly,
    qmd_index_path,
    snapshots_differ,
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
    """The connection helper must open the database in mode=ro."""
    db = _tiny_index(tmp_path, _empty_index_template)
    with pytest.raises(sqlite3.OperationalError):
        with closing(open_readonly(db)) as conn:
            conn.execute("DELETE FROM content")


def test_orphans_are_content_vectors_rows_with_no_backing_content(
    tmp_path: Path, _empty_index_template: Path
) -> None:
    """One orphan hash, two rows: an orphan is a vector row with no backing content."""
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
    """Every content_vectors row has a backing content row."""
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
    """An empty hash cannot match any row, by definition."""
    db = _tiny_index(tmp_path, _empty_index_template)
    assert is_embedded(db, "") is False


def test_is_embedded_is_a_single_row_check(tmp_path: Path, _empty_index_template: Path) -> None:
    """One row is enough — the count of rows is not the question."""
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
    """A registered path that does not exist on disk is drift."""
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
    """An empty registered path is the ``wiki_default`` case, not drift."""
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
    """Future messages append rather than overwrite."""
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


# --- document_drift (P-4) -----------------------------------------------
#
# The complement of ``collection_drift``: documents whose
# ``collection`` field has no ``store_collections`` row. The
# live observation on 2026-10-03 (PR #116 review) had a
# ``wiki_tag-filter-lib`` residue: 5 documents with no
# registry entry. ``collection_drift`` returned ``{}`` because
# it iterates ``store_collections`` only; the residue is
# structurally blind to that walker.


def test_document_drift_reports_unregistered_collection_with_documents(
    tmp_path: Path, _empty_index_template: Path
) -> None:
    """A collection name that has no registry row but holds documents is drift."""
    import sqlite3 as _sqlite3

    db = _tiny_index(tmp_path, _empty_index_template)
    with _sqlite3.connect(str(db)) as conn:
        # Three documents under an unregistered collection name.
        conn.executemany(
            "INSERT INTO documents(collection, path, title, hash, "
            "created_at, modified_at, active) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                ("wiki_tag-filter-lib", "a.md", "A", "ha", "t", "t", 1),
                ("wiki_tag-filter-lib", "b.md", "B", "hb", "t", "t", 1),
                ("wiki_tag-filter-lib", "c.md", "C", "hc", "t", "t", 1),
            ],
        )
        conn.commit()

    from lies.qmd.integrity import document_drift

    drift = document_drift(db)
    assert "wiki_tag-filter-lib" in drift, (
        "an unregistered collection name with rows must surface; "
        "collection_drift was blind to this case on the live index"
    )
    assert any("3 document" in m for m in drift["wiki_tag-filter-lib"]), (
        "the count must be in the message so the operator knows how "
        "much residue is in the orphan collection"
    )


def test_document_drift_is_empty_when_every_document_is_registered(
    tmp_path: Path, _empty_index_template: Path
) -> None:
    """A clean index has no document drift."""
    import sqlite3 as _sqlite3

    db = _tiny_index(tmp_path, _empty_index_template)
    coll_dir = tmp_path / "alpha"
    coll_dir.mkdir()
    with _sqlite3.connect(str(db)) as conn:
        conn.execute(
            "INSERT INTO store_collections(name, path) VALUES (?, ?)",
            ("alpha", str(coll_dir)),
        )
        conn.execute(
            "INSERT INTO documents(collection, path, title, hash, "
            "created_at, modified_at, active) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("alpha", "a.md", "A", "ha", "t", "t", 1),
        )
        conn.commit()

    from lies.qmd.integrity import document_drift

    assert document_drift(db) == {}


def test_qmd_index_path_resolves_under_xdg_cache_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _empty_index_template: Path
) -> None:
    """The default index path is ``$XDG_CACHE_HOME/qmd/index.sqlite``."""
    cache = tmp_path / "cache"
    monkeypatch.setenv("LIES_XDG_CACHE_HOME", str(cache))
    assert qmd_index_path() == cache / "qmd" / "index.sqlite"


def test_integrity_summary_returns_a_complete_snapshot(
    tmp_path: Path, _empty_index_template: Path
) -> None:
    """``integrity_summary`` is the JSON shape ``lies qmd status`` prints."""
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
    """An active document with no ``content_vectors`` row is a coverage gap."""
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
    """The active-vs-total split surfaces ``active=0`` rows."""
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


# ---------------------------------------------------------------------------
# live_index_snapshot / snapshots_differ — the session guard for the
# tag-filter fixture. The pre-fix guard's two-aggregate snapshot
# (collection names, active document count) was blind to the exact
# class of write that produced the four live-index orphans on
# 2026-10-03: a ``content_vectors`` row whose backing ``content`` and
# ``documents`` rows never landed (or were hard-deleted by an
# intervening ``qmd collection remove``). The four-field snapshot
# adds ``total_vectors`` and ``orphan_vectors``; both move on that
# write, and the unit tests below pin each field's discriminating
# power against a throwaway index, including a mutation test that
# fails the comparator when the orphan-write class is exercised.
# ---------------------------------------------------------------------------


def test_live_index_snapshot_returns_none_for_missing_index(
    tmp_path: Path,
) -> None:
    """A non-existent db returns ``None`` so the session guard can no-op."""
    missing = tmp_path / "does-not-exist.sqlite"
    assert live_index_snapshot(missing) is None


def test_live_index_snapshot_reads_four_aggregates(
    tmp_path: Path, _empty_index_template: Path
) -> None:
    """All four aggregates are populated, not just two."""
    db = _tiny_index(tmp_path, _empty_index_template)
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "INSERT INTO content(hash, doc, created_at) VALUES (?, ?, ?)",
            ("h1", "doc1", "2026-10-03T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO documents(collection, path, title, hash, "
            "created_at, modified_at, active) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "alpha",
                "p.md",
                "T",
                "h1",
                "2026-10-03T00:00:00Z",
                "2026-10-03T00:00:00Z",
                1,
            ),
        )
        conn.execute(
            "INSERT INTO store_collections(name, path) VALUES (?, ?)",
            ("alpha", "/collections/alpha"),
        )
        # Vector with backing content: does NOT count toward orphan_vectors.
        conn.execute(
            "INSERT INTO content_vectors(hash, seq, pos, model, "
            "embed_fingerprint, total_chunks, embedded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "h1",
                0,
                0,
                "m",
                "fp",
                1,
                "2026-10-03T00:00:00Z",
            ),
        )
        # Orphan vector: hash NOT IN content. Counts toward orphan_vectors.
        conn.execute(
            "INSERT INTO content_vectors(hash, seq, pos, model, "
            "embed_fingerprint, total_chunks, embedded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "orphan_hash",
                0,
                0,
                "m",
                "fp",
                1,
                "2026-10-03T00:00:00Z",
            ),
        )
        conn.commit()

    snap = live_index_snapshot(db)
    assert snap == LiveIndexSnapshot(
        collection_names=frozenset({"alpha"}),
        active_doc_count=1,
        total_vectors=2,
        orphan_vectors=1,
    )


def test_snapshots_differ_returns_no_change_for_identical_snapshots(
    tmp_path: Path, _empty_index_template: Path
) -> None:
    """Equality on every field ⇒ ``(False, "")`` — the comparator's no-op."""
    db = _tiny_index(tmp_path, _empty_index_template)
    snap = live_index_snapshot(db)
    changed, msg = snapshots_differ(snap, snap)
    assert changed is False
    assert msg == ""


def test_snapshots_differ_treats_either_side_none_as_no_op() -> None:
    """Either side ``None`` (no live index reachable) is a no-op, not a change."""
    none = None
    snap = LiveIndexSnapshot(
        collection_names=frozenset(),
        active_doc_count=0,
        total_vectors=0,
        orphan_vectors=0,
    )
    changed_a, _ = snapshots_differ(none, snap)
    changed_b, _ = snapshots_differ(snap, none)
    changed_c, _ = snapshots_differ(none, none)
    assert changed_a is False
    assert changed_b is False
    assert changed_c is False


def test_snapshots_differ_catches_an_orphan_vectors_increase(
    tmp_path: Path, _empty_index_template: Path
) -> None:
    """The mutation test the brief asks for."""
    db = _tiny_index(tmp_path, _empty_index_template)
    before = live_index_snapshot(db)

    # The orphan write: ``content_vectors`` row whose hash is not in
    # ``content`` and not in ``documents`` — the exact defect class the
    # four live-index orphans on 2026-10-03 belong to.
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "INSERT INTO content_vectors(hash, seq, pos, model, "
            "embed_fingerprint, total_chunks, embedded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "0073bb30aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                0,
                0,
                "m",
                "fp",
                1,
                "2026-10-03T07:42:53.369Z",
            ),
        )
        conn.commit()

    after = live_index_snapshot(db)
    changed, msg = snapshots_differ(before, after)
    assert changed, (
        "comparator returned no-change for an orphan write — the exact "
        "defect the four live-index orphans on 2026-10-03 belong to. "
        "The guard is blind again; ``orphan_vectors`` must stay in the snapshot."
    )
    assert "orphan vectors" in msg
    assert "before=" in msg and "after=" in msg


def test_snapshots_differ_catches_a_total_vectors_increase_with_backing_content(
    tmp_path: Path, _empty_index_template: Path
) -> None:
    """``total_vectors`` catches writes that ``orphan_vectors`` cannot."""
    db = _tiny_index(tmp_path, _empty_index_template)
    before = live_index_snapshot(db)

    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "INSERT INTO content(hash, doc, created_at) VALUES (?, ?, ?)",
            ("h", "d", "2026-10-03T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO content_vectors(hash, seq, pos, model, "
            "embed_fingerprint, total_chunks, embedded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "h",
                0,
                0,
                "m",
                "fp",
                1,
                "2026-10-03T00:00:00Z",
            ),
        )
        conn.commit()

    after = live_index_snapshot(db)
    changed, msg = snapshots_differ(before, after)
    assert changed
    assert "total vectors" in msg


def test_snapshots_differ_catches_a_collection_added_to_store_collections(
    tmp_path: Path, _empty_index_template: Path
) -> None:
    """``collection_names`` catches ``qmd collection add`` against the live index."""
    db = _tiny_index(tmp_path, _empty_index_template)
    before = live_index_snapshot(db)

    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "INSERT INTO store_collections(name, path) VALUES (?, ?)",
            ("alpha", "/collections/alpha"),
        )
        conn.commit()

    after = live_index_snapshot(db)
    changed, msg = snapshots_differ(before, after)
    assert changed
    assert "collections before=" in msg
    assert "added=['alpha']" in msg


def test_snapshots_differ_catches_an_active_doc_insertion(
    tmp_path: Path, _empty_index_template: Path
) -> None:
    """``active_doc_count`` catches ``qmd update`` that inserted documents."""
    db = _tiny_index(tmp_path, _empty_index_template)
    before = live_index_snapshot(db)

    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "INSERT INTO content(hash, doc, created_at) VALUES (?, ?, ?)",
            ("h", "d", "2026-10-03T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO documents(collection, path, title, hash, "
            "created_at, modified_at, active) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "alpha",
                "p.md",
                "T",
                "h",
                "2026-10-03T00:00:00Z",
                "2026-10-03T00:00:00Z",
                1,
            ),
        )
        conn.commit()

    after = live_index_snapshot(db)
    changed, msg = snapshots_differ(before, after)
    assert changed
    assert "active docs" in msg
