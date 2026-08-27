"""Unit tests for bootstrap_collection and ensure_wiki."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import pytest

from lies.collections.bootstrap import bootstrap_collection, ensure_wiki
from lies.collections.errors import (
    CollectionMismatch,
    CollectionNameRejected,
    WikiLayoutInitFailed,
)
from lies.collections.record import Collection, load_collection, save_collection
from lies.wiki.wiki import Wiki
from tests.conftest import make_wiki


@pytest.fixture
def wiki(tmp_path: Path) -> Wiki:
    root = tmp_path / "wiki"
    root.mkdir()
    return make_wiki(name="bootstrap-test", data_root=root)


def test_bare_scaffold_writes_standard_fields(wiki: Wiki) -> None:
    coll = bootstrap_collection(wiki, "alpha", "https://example.com/docs")
    expected_path = wiki.data_root / "raw" / "alpha"
    assert coll.name == "alpha"
    assert coll.source == "https://example.com/docs"
    assert coll.path == expected_path
    assert coll.tags == []
    assert coll.scraper_cmd is None
    assert coll.doc_path is None
    assert coll.mapper_model is None
    assert coll.language is None
    assert coll.version == "1"
    assert isinstance(coll.created_at, datetime)
    assert isinstance(coll.updated_at, datetime)
    assert coll.config == {}
    # Persisted to disk.
    assert load_collection(wiki, "alpha") == coll


def test_bare_scaffold_rejects_operator_chars(wiki: Wiki) -> None:
    with pytest.raises(CollectionNameRejected):
        bootstrap_collection(wiki, "a+b", "https://example.com")


def test_existing_matching_source_returns_existing(wiki: Wiki) -> None:
    first = bootstrap_collection(wiki, "alpha", "https://example.com/a")
    second = bootstrap_collection(wiki, "alpha", "https://example.com/a")
    # Reloaded from YAML on the second call, so ``second is not first`` --
    # equality on field values is what the contract guarantees.
    assert second == first
    assert load_collection(wiki, "alpha") == first


def test_existing_mismatched_source_raises(wiki: Wiki) -> None:
    bootstrap_collection(wiki, "alpha", "https://example.com/a")
    with pytest.raises(CollectionMismatch) as exc_info:
        bootstrap_collection(wiki, "alpha", "https://example.com/b")
    assert exc_info.value.existing_source == "https://example.com/a"
    assert exc_info.value.requested_source == "https://example.com/b"


def test_existing_mismatched_via_empty_existing_skipped(wiki: Wiki) -> None:
    """An existing YAML with empty source is treated as 'unknown'; no collision."""
    now = datetime.now(tz=UTC)
    save_collection(
        wiki,
        Collection(
            name="alpha",
            path=Path("/raw/alpha"),
            source="",
            tags=[],
            scraper_cmd=None,
            doc_path=None,
            mapper_model=None,
            language=None,
            version="1",
            created_at=now,
            updated_at=now,
            config={},
        ),
    )
    coll = bootstrap_collection(wiki, "alpha", "https://example.com/x")
    assert coll.source == ""  # kept existing


def test_wizard_true_raises_not_implemented(wiki: Wiki) -> None:
    with pytest.raises(NotImplementedError):
        bootstrap_collection(wiki, "alpha", "https://example.com", wizard=True)


def test_ensure_wiki_resolves_existing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    name = "existing-wiki"
    monkeypatch.setenv("LIES_WIKI_NAME", name)
    # Wiki.data_root_for = xdg.data_home() / LIES_DATA_SUBDIR / name.
    # The autouse fixture redirects XDG_DATA_HOME to tmp_path/xdg/data,
    # so the resolved data root lives there.
    root = tmp_path / "xdg" / "data" / "lies" / name
    root.mkdir(parents=True)
    wiki = ensure_wiki(name)
    assert wiki.data_root == root


def test_ensure_wiki_auto_inits_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    name = "fresh-wiki"
    monkeypatch.setenv("LIES_WIKI_NAME", name)
    monkeypatch.setattr("lies.cli.xdg.data_home", lambda: tmp_path)
    monkeypatch.setattr("lies.cli.xdg.config_home", lambda: tmp_path)
    monkeypatch.setattr("lies.cli.xdg.cache_home", lambda: tmp_path)
    monkeypatch.setattr("lies.cli.xdg.state_home", lambda: tmp_path)
    monkeypatch.setattr("lies.wiki.wiki.xdg.runtime_dir_for", lambda n: tmp_path / "run" / n)
    wiki = ensure_wiki(name)
    assert wiki.data_root.exists()
    assert (wiki.data_root / "raw").exists()
    assert (wiki.data_root / "wiki").exists()


def test_ensure_wiki_propagates_layout_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = "fail-wiki"
    monkeypatch.setenv("LIES_WIKI_NAME", name)
    monkeypatch.setattr("lies.cli.xdg.data_home", lambda: tmp_path)
    monkeypatch.setattr("lies.cli.xdg.config_home", lambda: tmp_path)
    monkeypatch.setattr("lies.cli.xdg.cache_home", lambda: tmp_path)
    monkeypatch.setattr("lies.cli.xdg.state_home", lambda: tmp_path)
    monkeypatch.setattr("lies.wiki.wiki.xdg.runtime_dir_for", lambda n: tmp_path / "run" / n)
    with (
        mock.patch(
            "lies.collections.bootstrap.WikiLayout.init",
            side_effect=PermissionError("denied"),
        ),
        pytest.raises(WikiLayoutInitFailed) as exc_info,
    ):
        ensure_wiki(name)
    assert exc_info.value.wiki_name == name
    assert isinstance(exc_info.value.__cause__, PermissionError)
