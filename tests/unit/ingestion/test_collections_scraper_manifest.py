import hashlib
from pathlib import Path

import pytest

from lies.collections.errors import CollectionConfigInvalid
from lies.collections.scraper_manifest import FileEntry, ScraperManifest


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def test_write_then_read_roundtrip(tmp_path: Path) -> None:
    entries = [
        FileEntry(path="docs/x.md", sha256=_sha("x")),
        FileEntry(path="docs/y.md", sha256=_sha("y")),
    ]
    out = ScraperManifest.write(tmp_path, entries)
    assert out.exists()
    read = ScraperManifest.read(tmp_path)
    assert read == entries


def test_read_missing_returns_empty(tmp_path: Path) -> None:
    assert ScraperManifest.read(tmp_path) == []


def test_read_malformed_raises(tmp_path: Path) -> None:
    (tmp_path / "manifest.json").write_text("not json", encoding="utf-8")
    with pytest.raises(CollectionConfigInvalid):
        ScraperManifest.read(tmp_path)
