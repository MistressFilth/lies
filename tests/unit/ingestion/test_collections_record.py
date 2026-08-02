"""Tests for collection records and YAML persistence."""

from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from lies.collections.errors import (
    CollectionConfigInvalid,
    CollectionNameRejected,
    CollectionNotFound,
)
from lies.collections.record import (
    CONFIG_DIR_NAME,
    Collection,
    load_collection,
    save_collection,
)


@pytest.fixture
def wiki(tmp_path: Path) -> Path:
    config_dir = tmp_path / ".lies" / CONFIG_DIR_NAME
    config_dir.mkdir(parents=True)
    return tmp_path


def _write(wiki: Path, name: str, payload: dict[str, object]) -> None:
    path = wiki / ".lies" / CONFIG_DIR_NAME / f"{name}.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")


def _good(name: str = "cpython") -> dict[str, object]:
    return {
        "name": name,
        "path": "./raw/cpython",
        "source": "https://github.com/python/cpython",
        "tags": ["python", "language"],
        "scraper_cmd": None,
        "doc_path": None,
        "mapper_model": None,
        "language": "en",
        "version": "1.0.0",
        "created_at": "2026-08-01T00:00:00Z",
        "updated_at": "2026-08-01T00:00:00Z",
    }


def test_collection_loads_valid_config(wiki: Path) -> None:
    _write(wiki, "cpython", _good())

    collection = load_collection(wiki, "cpython")

    assert collection.name == "cpython"
    assert collection.tags == ["python", "language"]
    assert collection.language == "en"


@pytest.mark.parametrize("operator", ["+", "&", "|", "-"])
def test_collection_rejects_operator_chars(wiki: Path, operator: str) -> None:
    name = f"bad{operator}name"
    _write(wiki, name, _good(name))

    with pytest.raises(CollectionNameRejected):
        load_collection(wiki, name)


def test_collection_not_found(wiki: Path) -> None:
    with pytest.raises(CollectionNotFound):
        load_collection(wiki, "missing")


def test_collection_invalid_yaml(wiki: Path) -> None:
    (wiki / ".lies" / CONFIG_DIR_NAME / "broken.yaml").write_text(
        "name: [\nbroken", encoding="utf-8"
    )

    with pytest.raises(CollectionConfigInvalid):
        load_collection(wiki, "broken")


def test_collection_qmd_name_matches_name() -> None:
    collection = Collection(
        name="cpython",
        path=Path("/tmp/raw/cpython"),
        source="https://example.com",
        tags=[],
        scraper_cmd=None,
        doc_path=None,
        mapper_model=None,
        language=None,
        version="1.0.0",
        created_at=datetime.now(tz=timezone.utc),
        updated_at=datetime.now(tz=timezone.utc),
    )

    assert collection.qmd_name() == "cpython"


def test_save_then_load_roundtrip(wiki: Path, tmp_path: Path) -> None:
    collection = Collection(
        name="vuejs",
        path=tmp_path / "raw" / "vuejs",
        source="https://vuejs.org",
        tags=["vue"],
        scraper_cmd=None,
        doc_path=None,
        mapper_model=None,
        language=None,
        version="1.0.0",
        created_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )

    save_collection(wiki, collection)
    loaded = load_collection(wiki, "vuejs")

    assert loaded.tags == ["vue"]


def test_collection_round_trips_config(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    from lies.collections.record import Collection, load_collection, save_collection

    now = datetime.now(tz=timezone.utc)
    c = Collection(
        name="htmx",
        path=tmp_path / "raw" / "htmx",
        source="https://github.com/bigskysoftware/htmx",
        tags=["docs"],
        scraper_cmd=None,
        doc_path=None,
        mapper_model=None,
        language="en",
        version="1.0.0",
        created_at=now,
        updated_at=now,
        config={"sphinx_includes": ["docs/**/*.rst"], "sphinx_excludes": ["_templates/**"]},
    )
    save_collection(tmp_path, c)
    loaded = load_collection(tmp_path, "htmx")
    assert loaded.config == {
        "sphinx_includes": ["docs/**/*.rst"],
        "sphinx_excludes": ["_templates/**"],
    }


def test_collection_config_defaults_to_empty_dict(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    from lies.collections.record import load_collection

    # Build a minimal Collection, then write a YAML that omits the config field.
    payload = {
        "name": "no_cfg",
        "path": str(tmp_path / "raw" / "no_cfg"),
        "source": "",
        "tags": [],
        "scraper_cmd": None,
        "doc_path": None,
        "mapper_model": None,
        "language": None,
        "version": "1.0.0",
        "created_at": datetime.now(tz=timezone.utc).isoformat(),
        "updated_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    cfg_dir = tmp_path / ".lies" / "collections"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "no_cfg.yaml").write_text(yaml.safe_dump(payload, sort_keys=True), encoding="utf-8")
    loaded = load_collection(tmp_path, "no_cfg")
    assert loaded.config == {}
