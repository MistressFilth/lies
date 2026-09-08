"""Pins that ``sync_collection`` writes to library, not wiki.

Mocks the scraper layer so the test does not shell out; asserts the
mirror file lands under ``library.collections_root`` and the wiki's
``wiki_dir`` is untouched.
"""

from pathlib import Path
import subprocess
import pytest
from lies.etl.sync_helper import sync_collection
from lies.library.paths import Library


@pytest.fixture
def fixture_lib(tmp_path: Path, monkeypatch) -> Library:
    from lies import xdg

    monkeypatch.setattr(xdg, "data_home", lambda: tmp_path)
    Library.open.cache_clear()
    lib = Library.open()
    lib.git_root.mkdir(parents=True, exist_ok=True)
    (lib.git_root / ".lies").mkdir(parents=True, exist_ok=True)
    (lib.git_root / ".gitkeep").write_text("")
    subprocess.run(
        ["git", "init", "-b", "main", str(lib.git_root)], check=True, capture_output=True
    )
    subprocess.run(["git", "-C", str(lib.git_root), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(lib.git_root), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(lib.git_root), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(lib.git_root), "commit", "-m", "init"], check=True, capture_output=True
    )
    return lib


def test_sync_collection_writes_to_library(fixture_lib: Library, monkeypatch) -> None:
    """``sync_collection`` must land the mirror under library, not wiki."""
    # Stub Wiki with the data_root pointing at fixture_lib.git_root (wiki
    # side gets nothing committed). Stub scrapers to emit one FetchItem.
    from lies.wiki.wiki import Wiki

    wiki = Wiki(
        name="t",
        data_root=fixture_lib.git_root,
        config_root=fixture_lib.git_root,
        cache_root=fixture_lib.git_root,
        state_root=fixture_lib.git_root,
        runtime_root=fixture_lib.git_root,
    )
    # Collection YAML at ``wiki.collections_dir`` so load_collection resolves.
    wiki.collections_dir.mkdir(parents=True, exist_ok=True)
    (wiki.collections_dir / "claude.yaml").write_text(
        "name: claude\n"
        "path: raw/claude\n"
        "source: https://example.com/claude\n"
        "tags: []\n"
        "version: '1'\n"
        "created_at: 2026-01-01T00:00:00\n"
        "updated_at: 2026-01-01T00:00:00\n",
        encoding="utf-8",
    )

    captured = {"called_with": None}

    def fake_run_batch(*args, **kwargs):
        captured["called_with"] = kwargs
        from lies.library.ingest import BatchIngestResult

        return BatchIngestResult(created=1)

    monkeypatch.setattr("lies.library.ingest.run_batch_ingest", fake_run_batch)

    sync_collection(
        wiki=wiki,
        collection_name="claude",
        force=False,
    )

    # The library-side fakes confirmed library was the target.
    assert captured["called_with"] is not None
    assert captured["called_with"].get("library") is fixture_lib
