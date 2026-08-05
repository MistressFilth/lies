import hashlib
from pathlib import Path

import pytest

from lies.collections.errors import CollectionConfigInvalid
from lies.collections.scraper_manifest import FileEntry, ScraperManifest
from lies.wiki.wiki import Wiki


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _wiki(tmp_path: Path) -> Wiki:
    wiki = Wiki(
        name="test",
        data_root=tmp_path / "data",
        config_root=tmp_path / "config",
        cache_root=tmp_path / "cache",
        state_root=tmp_path / "state",
        runtime_root=tmp_path / "runtime",
    )
    wiki.cache_root.mkdir(parents=True, exist_ok=True)
    return wiki


def test_write_then_read_roundtrip(tmp_path: Path) -> None:
    wiki = _wiki(tmp_path)
    entries = [
        FileEntry(path="docs/x.md", sha256=_sha("x")),
        FileEntry(path="docs/y.md", sha256=_sha("y")),
    ]
    out = ScraperManifest.write(wiki, "sample", entries)
    assert out.exists()
    read = ScraperManifest.read(wiki, "sample")
    assert read == entries


def test_read_missing_returns_empty(tmp_path: Path) -> None:
    assert ScraperManifest.read(_wiki(tmp_path), "sample") == []


def test_read_malformed_raises(tmp_path: Path) -> None:
    wiki = _wiki(tmp_path)
    (ScraperManifest.manifest_dir(wiki, "sample")).mkdir(parents=True, exist_ok=True)
    (ScraperManifest.manifest_dir(wiki, "sample") / "manifest.json").write_text(
        "not json", encoding="utf-8"
    )
    with pytest.raises(CollectionConfigInvalid):
        ScraperManifest.read(wiki, "sample")
