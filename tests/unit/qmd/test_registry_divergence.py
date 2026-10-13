"""An emptied collection registry is one event, not N drift entries."""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from pathlib import Path

import pytest

from lies.qmd.integrity import integrity_summary, registry_divergence

_SCHEMA = """
CREATE TABLE store_collections (
    name TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    pattern TEXT NOT NULL DEFAULT '**/*.md'
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
    active INTEGER NOT NULL DEFAULT 1
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
def _template() -> Path:
    import os

    fd, name = tempfile.mkstemp(suffix=".sqlite", prefix="lies-registry-")
    os.close(fd)
    template = Path(name)
    with sqlite3.connect(str(template)) as conn:
        conn.executescript(_SCHEMA)
    return template


def _index(tmp_path: Path, template: Path) -> Path:
    db = tmp_path / "index.sqlite"
    shutil.copyfile(template, db)
    return db


def _documents(db: Path, *collections: str) -> None:
    """One document per collection name."""
    with sqlite3.connect(str(db)) as conn:
        for i, name in enumerate(collections):
            conn.execute(
                "INSERT INTO documents "
                "(collection, path, title, hash, created_at, modified_at) "
                "VALUES (?, ?, ?, ?, '2026-10-04', '2026-10-04')",
                (name, f"{name}/page.md", name, f"{i:064x}"),
            )


def _register(db: Path, *names: str) -> None:
    with sqlite3.connect(str(db)) as conn:
        for name in names:
            conn.execute(
                "INSERT INTO store_collections(name, path) VALUES (?, ?)",
                (name, f"/somewhere/{name}"),
            )


# ---------------------------------------------------------------------
# The detected state
# ---------------------------------------------------------------------


def test_an_empty_registry_with_documents_is_named_as_divergence(
    tmp_path: Path, _template: Path
) -> None:
    """The state as measured: registry 0, documents referencing 117."""
    db = _index(tmp_path, _template)
    _documents(db, "claude_code", "mermaid", "typer")

    found = registry_divergence(db)

    assert found is not None, "an emptied registry went unreported"
    assert found["registered"] == 0
    assert found["referenced"] == 3
    assert found["documents"] == 3


def test_the_finding_names_the_cause_and_a_remedy(tmp_path: Path, _template: Path) -> None:
    """A diagnosis an operator can act on, not a restatement."""
    db = _index(tmp_path, _template)
    _documents(db, "mermaid")

    found = registry_divergence(db)
    assert found is not None

    blob = " ".join(str(v) for v in found.values())
    assert "syncConfigToDb" in blob, f"the cause must be named: {found}"
    assert "index.yml" in blob, f"the config that governs it must be named: {found}"
    assert "store_collections" in blob, f"the emptied table must be named: {found}"


# ---------------------------------------------------------------------
# The states that must NOT be flagged
# ---------------------------------------------------------------------


def test_a_healthy_index_reports_no_divergence(tmp_path: Path, _template: Path) -> None:
    """The common case stays quiet — a finding that is always present is noise."""
    db = _index(tmp_path, _template)
    _documents(db, "mermaid", "typer")
    _register(db, "mermaid", "typer")

    assert registry_divergence(db) is None


def test_a_fresh_index_reports_no_divergence(tmp_path: Path, _template: Path) -> None:
    """qmd never run. An empty registry and no documents is the normal state."""
    db = _index(tmp_path, _template)

    assert registry_divergence(db) is None


def test_one_missing_collection_is_not_divergence(tmp_path: Path, _template: Path) -> None:
    """Ordinary drift stays ordinary drift."""
    db = _index(tmp_path, _template)
    _documents(db, "mermaid", "stray")
    _register(db, "mermaid")

    assert registry_divergence(db) is None


# ---------------------------------------------------------------------
# Surfacing — the reason this exists
# ---------------------------------------------------------------------


def test_the_summary_carries_the_diagnosis(tmp_path: Path, _template: Path) -> None:
    """``lies qmd status`` is JSON, so the diagnosis has to be in it."""
    db = _index(tmp_path, _template)
    _documents(db, "claude_code", "mermaid")

    summary = integrity_summary(db)

    assert "registry_divergence" in summary, (
        f"the status payload must name the divergence; keys: {sorted(summary)}"
    )
    assert summary["registry_divergence"] is not None


def test_the_summary_reports_none_when_healthy(tmp_path: Path, _template: Path) -> None:
    """The key is always present, so a consumer can rely on its shape."""
    db = _index(tmp_path, _template)
    _documents(db, "mermaid")
    _register(db, "mermaid")

    summary = integrity_summary(db)

    assert summary["registry_divergence"] is None


def test_the_raw_drift_is_still_reported_alongside(tmp_path: Path, _template: Path) -> None:
    """The diagnosis supplements the data; it does not replace it."""
    db = _index(tmp_path, _template)
    _documents(db, "claude_code", "mermaid")

    summary = integrity_summary(db)

    assert summary["registry_divergence"] is not None
    assert set(summary["document_drift"]) == {"claude_code", "mermaid"}


def test_the_summary_stays_json_serialisable(tmp_path: Path, _template: Path) -> None:
    """``lies qmd status`` is printed with ``json.dumps``."""
    import json

    db = _index(tmp_path, _template)
    _documents(db, "mermaid")

    json.dumps(integrity_summary(db))  # must not raise
