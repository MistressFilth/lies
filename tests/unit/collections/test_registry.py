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
