"""Unit tests for the collection registry loader and writer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from lies.collections.errors import (
    RegistryCorrupt,
    RegistryVersionUnsupported,
)
from lies.collections.registry import Registry
from lies.memory.models import WikiCollectionRef
from tests.conftest import make_wiki

if TYPE_CHECKING:
    from lies.wiki.wiki import Wiki


def _ref(name: str) -> WikiCollectionRef:
    return WikiCollectionRef(
        collection_id=name,
        root=PurePosixPath(f"/raw/{name}"),
        qmd_collection=name,
        schema_path=PurePosixPath(f"/cfg/{name}/schema.md"),
    )


from pathlib import PurePosixPath


def _wiki(tmp_path: Path) -> Wiki:
    return make_wiki(name="reg", data_root=tmp_path / "wiki")


def test_load_missing_file_returns_empty_registry(tmp_path: Path) -> None:
    wiki = _wiki(tmp_path)
    reg = Registry.load(wiki)
    assert reg.version == 1
    assert reg.collections == {}


def test_load_valid_file_roundtrips(tmp_path: Path) -> None:
    wiki = _wiki(tmp_path)
    wiki.registry_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "collections": {
            "htmx": {
                "collection_id": "htmx",
                "root": "/raw/htmx",
                "qmd_collection": "htmx",
                "schema_path": "/cfg/reg/schema.md",
            }
        },
    }
    wiki.registry_path.write_text(json.dumps(payload), encoding="utf-8")
    reg = Registry.load(wiki)
    assert reg.collections["htmx"].root == PurePosixPath("/raw/htmx")
    assert reg.collections["htmx"].schema_path == PurePosixPath("/cfg/reg/schema.md")


def test_load_rejects_unknown_version(tmp_path: Path) -> None:
    wiki = _wiki(tmp_path)
    wiki.registry_path.parent.mkdir(parents=True, exist_ok=True)
    wiki.registry_path.write_text(json.dumps({"version": 999, "collections": {}}), encoding="utf-8")
    with pytest.raises(RegistryVersionUnsupported) as exc:
        Registry.load(wiki)
    assert exc.value.found == 999
    assert exc.value.supported == 1


def test_load_rejects_corrupt_json(tmp_path: Path) -> None:
    wiki = _wiki(tmp_path)
    wiki.registry_path.parent.mkdir(parents=True, exist_ok=True)
    wiki.registry_path.write_text("{not json", encoding="utf-8")
    with pytest.raises(RegistryCorrupt):
        Registry.load(wiki)


def test_load_rejects_missing_version_field(tmp_path: Path) -> None:
    wiki = _wiki(tmp_path)
    wiki.registry_path.parent.mkdir(parents=True, exist_ok=True)
    wiki.registry_path.write_text(json.dumps({"collections": {}}), encoding="utf-8")
    with pytest.raises(RegistryVersionUnsupported):
        Registry.load(wiki)


def test_load_rejects_bool_version(tmp_path: Path) -> None:
    """``True``/``False`` are ``int`` subclasses in Python; reject explicitly."""
    wiki = _wiki(tmp_path)
    wiki.registry_path.parent.mkdir(parents=True, exist_ok=True)
    wiki.registry_path.write_text(
        json.dumps({"version": True, "collections": {}}), encoding="utf-8"
    )
    with pytest.raises(RegistryVersionUnsupported):
        Registry.load(wiki)


def test_save_then_load_roundtrips(tmp_path: Path) -> None:
    wiki = _wiki(tmp_path)
    reg = Registry(
        collections={"htmx": _ref("htmx"), "lask": _ref("lask")},
    )
    Registry.save(wiki, reg)
    loaded = Registry.load(wiki)
    assert loaded.collections.keys() == {"htmx", "lask"}


def test_save_writes_to_temp_then_renames(tmp_path: Path) -> None:
    wiki = _wiki(tmp_path)
    reg = Registry(collections={"htmx": _ref("htmx")})
    Registry.save(wiki, reg)
    # No temp file should linger after a successful save.
    assert not (wiki.registry_path.with_suffix(".json.tmp")).exists()
    # Final file is the canonical JSON.
    payload = json.loads(wiki.registry_path.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert "htmx" in payload["collections"]


def test_save_creates_parent_directory(tmp_path: Path) -> None:
    wiki = _wiki(tmp_path)
    assert not wiki.registry_path.parent.exists()
    Registry.save(wiki, Registry(collections={"x": _ref("x")}))
    assert wiki.registry_path.exists()


def test_save_replaces_existing_file_atomically(tmp_path: Path) -> None:
    wiki = _wiki(tmp_path)
    Registry.save(wiki, Registry(collections={"old": _ref("old")}))
    Registry.save(wiki, Registry(collections={"new": _ref("new")}))
    loaded = Registry.load(wiki)
    assert set(loaded.collections.keys()) == {"new"}


def test_save_cleans_temp_on_failure(tmp_path: Path, monkeypatch) -> None:
    wiki = _wiki(tmp_path)
    monkeypatch.setattr(
        "os.replace",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("boom")),
    )
    from lies.collections.errors import RegistryWriteFailed

    with pytest.raises(RegistryWriteFailed):
        Registry.save(wiki, Registry(collections={"x": _ref("x")}))
    assert not (wiki.registry_path.with_suffix(".json.tmp")).exists()


def test_merge_unions_by_collection_id() -> None:
    a = Registry(collections={"x": _ref("x"), "shared": _ref("shared")})
    b = Registry(collections={"y": _ref("y"), "shared": _ref("shared")})
    merged = Registry.merge(a, b)
    assert set(merged.collections.keys()) == {"x", "y", "shared"}
    # Last-arg wins on collisions so callers can layer writes deterministically.
    assert merged.collections["shared"] is b.collections["shared"]


def test_merge_with_empty_registry_keeps_other_side() -> None:
    a = Registry(collections={"x": _ref("x")})
    b = Registry(collections={})
    assert set(Registry.merge(a, b).collections.keys()) == {"x"}
    assert set(Registry.merge(b, a).collections.keys()) == {"x"}


def test_filter_stale_drops_entries_with_missing_yaml(tmp_path: Path) -> None:
    wiki = _wiki(tmp_path)
    wiki.collections_dir.mkdir(parents=True, exist_ok=True)
    (wiki.collections_dir / "alive.yaml").write_text("name: alive\n", encoding="utf-8")
    reg = Registry(collections={"alive": _ref("alive"), "ghost": _ref("ghost")})
    live = Registry.filter_stale(reg, wiki)
    assert set(live.collections.keys()) == {"alive"}


def test_filter_stale_keeps_all_when_all_yamls_exist(tmp_path: Path) -> None:
    wiki = _wiki(tmp_path)
    wiki.collections_dir.mkdir(parents=True, exist_ok=True)
    for cid in ("a", "b"):
        (wiki.collections_dir / f"{cid}.yaml").write_text(f"name: {cid}\n", encoding="utf-8")
    reg = Registry(collections={"a": _ref("a"), "b": _ref("b")})
    live = Registry.filter_stale(reg, wiki)
    assert set(live.collections.keys()) == {"a", "b"}
