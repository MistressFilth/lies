"""Unit tests for the collection record loader and atomic writer."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from unittest import mock

import pytest

from lies.collections.errors import CollectionWriteFailed
from lies.collections.record import (
    Collection,
    load_collection,
    save_collection,
)
from tests.conftest import make_wiki


def _collection(name: str = "alpha") -> Collection:
    return Collection(
        name=name,
        path=PurePosixPath(f"/raw/{name}"),
        source="https://example.com",
        tags=["t"],
        scraper_cmd=None,
        doc_path=None,
        mapper_model=None,
        language=None,
        version="1",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        config={},
    )


def test_save_collection_writes_atomically(tmp_path: Path) -> None:
    wiki = make_wiki(name="atomic", data_root=tmp_path / "wiki")
    save_collection(wiki, _collection("alpha"))
    target = Collection.config_path(wiki, "alpha")
    sibling_tmp = target.with_suffix(target.suffix + ".tmp")
    # Sibling tmp must share the target's parent directory (atomic-rename
    # contract: tmp is renamed into place on the same filesystem). This
    # catches accidental tempfile.NamedTemporaryFile() usage without
    # coupling the assertion to pytest's tmp_path parent directory.
    assert sibling_tmp.parent == target.parent
    assert sibling_tmp.parent == Collection.config_path(wiki, "alpha").parent
    # Final target must exist; tmp must not
    assert target.exists()
    assert not sibling_tmp.exists()
    # Reload round-trip
    loaded = load_collection(wiki, "alpha")
    assert loaded.name == "alpha"
    assert loaded.source == "https://example.com"


def test_save_collection_cleans_tmp_on_write_failure(tmp_path: Path) -> None:
    wiki = make_wiki(name="atomic", data_root=tmp_path / "wiki")
    # fsync is module-level `os.fsync` and runs after tmp.open succeeds
    # but before os.replace. Forcing it to fail exercises the cleanup
    # branch without depending on the local `tmp` variable (which is
    # not patchable via string path).
    with (
        mock.patch("os.fsync", side_effect=OSError("disk full")),
        pytest.raises(CollectionWriteFailed) as excinfo,
    ):
        save_collection(wiki, _collection("alpha"))
    assert "disk full" in str(excinfo.value)
    # No tmp file left behind
    target = Collection.config_path(wiki, "alpha")
    sibling_tmp = target.with_suffix(target.suffix + ".tmp")
    assert not sibling_tmp.exists()
    assert not target.exists()


def test_save_collection_fsyncs_before_replace(tmp_path: Path) -> None:
    wiki = make_wiki(name="atomic", data_root=tmp_path / "wiki")
    order: list[str] = []
    real_replace = __import__("os").replace

    def fake_fsync(fd: int) -> None:
        order.append("fsync")

    def fake_replace(src: str, dst: str) -> None:
        order.append("replace")
        real_replace(src, dst)

    with (
        mock.patch("os.fsync", side_effect=fake_fsync),
        mock.patch("os.replace", side_effect=fake_replace),
    ):
        save_collection(wiki, _collection("alpha"))
    assert order == ["fsync", "replace"]


def test_save_collection_in_memory_only_skips_io(tmp_path: Path) -> None:
    wiki = make_wiki(name="atomic", data_root=tmp_path / "wiki")
    save_collection(wiki, _collection("alpha"), in_memory_only=True)
    target = Collection.config_path(wiki, "alpha")
    assert not target.exists()
