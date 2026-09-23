"""Unit tests for ``lies status`` library-state surfacing (Task 15).

``lies status`` gains a ``library:`` line that reports the catalog
row count by section (``library`` + ``library-migrated``). The line
must surface even when no wiki is registered yet (the library is
independent of any specific wiki).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from lies.cli import app

runner = CliRunner()

pytestmark = pytest.mark.slow


@pytest.fixture(autouse=True)
def _mock_external_services(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub git + qmd so the status command's library path never shells out."""
    monkeypatch.setattr(
        "lies.library.writer.atomic_commit",
        lambda *_a, **_kw: "deadbeef" * 5,
    )
    monkeypatch.setattr("lies.library.writer.qmd_update", lambda *_a, **_kw: None)
    monkeypatch.setattr("lies.library.writer.qmd_collection_add_or_update", lambda *_a, **_kw: None)
    monkeypatch.setattr("lies.library.writer.qmd_embed", lambda *_a, **_kw: None)


def _setup_library(tmp_path: Path, monkeypatch) -> object:
    """Provision an isolated library root with the on-disk shape the CLI expects.

    No real ``git init`` runs — the autouse ``_mock_external_services``
    fixture stubs the commit path. The ``.gitkeep`` placeholder is
    still created so the catalog DB gets a sibling file inside the
    git_root (otherwise ``Library.open`` complains the dir is untracked
    in ways the CLI doesn't expect).
    """
    from lies import xdg
    from lies.library.paths import Library

    lib_root = tmp_path / "lib"
    lib_root.mkdir()
    (lib_root / ".lies").mkdir()
    (lib_root / "collections").mkdir()
    monkeypatch.setattr(xdg, "data_home", lambda: lib_root)

    Library.open.cache_clear()
    lib = Library.open()
    lib.git_root.mkdir(parents=True, exist_ok=True)
    (lib.git_root / ".gitkeep").write_text("placeholder\n")
    return lib


def _stub_wiki(tmp_path: Path, monkeypatch) -> None:
    """Stub ``resolve_wiki`` so the wiki section doesn't fail with WikiNotRegistered."""
    from lies.wiki.wiki import Wiki

    wiki = Wiki(
        name="default",
        data_root=tmp_path / "wiki",
        config_root=tmp_path / "config",
        cache_root=tmp_path / "cache",
        state_root=tmp_path / "state",
        runtime_root=tmp_path / "runtime",
    )
    (tmp_path / "wiki" / "wiki").mkdir(parents=True)
    monkeypatch.setattr("lies.cli.resolve_wiki", lambda _name=None: wiki)


def test_status_reports_library_catalog_count(tmp_path: Path, monkeypatch) -> None:
    """``lies status`` surfaces a ``library:`` line with catalog counts.

    Populates the catalog with BOTH ``section="library"`` and
    ``section="library-migrated"`` rows so the migrated tally is non-zero
    — a regression that drops the migrated count or conflates the two
    would surface as a substring mismatch on ``, 1 migrated`` below.
    """
    from lies.library.catalog import (
        LibraryCatalogPage,
        open_catalog,
        upsert_page,
    )

    lib = _setup_library(tmp_path, monkeypatch)
    _stub_wiki(tmp_path, monkeypatch)

    # Populate the catalog so the migrated tally is meaningful.
    conn = open_catalog(lib)
    try:
        upsert_page(
            conn,
            LibraryCatalogPage(
                slug="claude/x",
                title="X",
                type="",
                source_pkg="claude",
                section="library",
                updated="2026-01-01",
                hash="h1",
                derived_from="",
            ),
        )
        upsert_page(
            conn,
            LibraryCatalogPage(
                slug="wiki-old/y",
                title="Y",
                type="",
                source_pkg="wiki-old",
                section="library-migrated",
                updated="2026-01-02",
                hash="h2",
                derived_from="",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    result = runner.invoke(app, ["status"], env={"LIES_WIKI_NAME": "default"})
    # ``section=library`` is the library-line substring; the wiki-side
    # ``catalog:`` line only says ``catalog:`` (no ``section=``), so this
    # assertion pins the library line specifically (not the wiki catalog
    # block that pre-existed).
    out = result.stdout
    assert "section=library" in out, f"library line missing from status output: {out!r}"
    # Pin both counts so a regression that drops the migrated tally or
    # conflates sections is caught — ``, 1 migrated`` proves the
    # ``library-migrated`` section is queried and reported distinctly.
    assert "1 pages in section=library" in out, f"library count missing: {out!r}"
    assert "1 migrated" in out, f"migrated count missing: {out!r}"


def test_status_reports_zero_migrated_when_only_library_section(
    tmp_path: Path, monkeypatch
) -> None:
    """``lies status`` prints ``0 migrated`` when only ``library`` rows exist.

    Pins the unpopulated-quarantine case so a regression that always
    reports ``0`` (or always reports ``N``) for the migrated tally is
    caught regardless of how the populated case changes.
    """
    from lies.library.catalog import (
        LibraryCatalogPage,
        open_catalog,
        upsert_page,
    )

    lib = _setup_library(tmp_path, monkeypatch)
    _stub_wiki(tmp_path, monkeypatch)

    # Populate ONLY ``section="library"``; no migrated rows.
    conn = open_catalog(lib)
    try:
        upsert_page(
            conn,
            LibraryCatalogPage(
                slug="claude/x",
                title="X",
                type="",
                source_pkg="claude",
                section="library",
                updated="2026-01-01",
                hash="h1",
                derived_from="",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    result = runner.invoke(app, ["status"], env={"LIES_WIKI_NAME": "default"})
    out = result.stdout
    assert "section=library" in out, f"library line missing: {out!r}"
    # Exactly zero migrated; substring "0 migrated" pins the unpopulated case.
    assert "0 migrated" in out, f"zero-migrated indicator missing: {out!r}"
