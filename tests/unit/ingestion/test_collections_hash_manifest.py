import hashlib
import json
from pathlib import Path

import pytest

from lies.collections.hash_manifest import HashManifest
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
    wiki.hashes_dir.mkdir(parents=True, exist_ok=True)
    return wiki


def test_manifest_starts_empty(tmp_path: Path) -> None:
    m = HashManifest(_wiki(tmp_path), "cpython")
    assert m.read() == {}


def test_manifest_update_then_compare(tmp_path: Path) -> None:
    m = HashManifest(_wiki(tmp_path), "cpython")
    h = _sha("hello")
    m.update("concepts/x.md", h)
    assert m.compare("concepts/x.md", h) is True
    assert m.compare("concepts/x.md", _sha("other")) is False


def test_manifest_persists_to_disk(tmp_path: Path) -> None:
    wiki = _wiki(tmp_path)
    m = HashManifest(wiki, "cpython")
    m.update("a.md", _sha("aaa"))
    m.flush()
    on_disk = json.loads((wiki.hashes_dir / "cpython.json").read_text(encoding="utf-8"))
    assert on_disk == {"a.md": _sha("aaa")}


def test_manifest_reads_existing(tmp_path: Path) -> None:
    wiki = _wiki(tmp_path)
    (wiki.hashes_dir / "cpython.json").write_text(
        json.dumps({"a.md": _sha("aaa")}), encoding="utf-8"
    )
    m = HashManifest(wiki, "cpython")
    assert m.compare("a.md", _sha("aaa")) is True


def test_snapshot_writes_copy(tmp_path: Path) -> None:
    wiki = _wiki(tmp_path)
    m = HashManifest(wiki, "cpython")
    m.update("a.md", _sha("aaa"))
    m.flush()
    snap = m.snapshot()
    assert snap.exists()
    assert "pre-sync" in snap.name


def test_snapshot_then_restore_recovers_state(tmp_path: Path) -> None:
    wiki = _wiki(tmp_path)
    m = HashManifest(wiki, "cpython")
    m.update("a.md", _sha("aaa"))
    m.flush()
    snap = m.snapshot()
    m.update("a.md", _sha("bbb"))
    m.flush()
    m.restore(snap)
    assert m.compare("a.md", _sha("aaa")) is True


def test_rejects_non_wiki(tmp_path: Path) -> None:
    with pytest.raises(TypeError):
        HashManifest(tmp_path, "cpython")
