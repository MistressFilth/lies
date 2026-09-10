"""CLI tests for the ``lies collections enrich-tags`` dry-run helper.

The helper walks the wiki's collection YAMLs and prints a
``lies collections modify <name> --set tags=<comma-separated>`` hint for
every collection whose ``tags`` field is empty or missing. The dry-run
mode (the default) must never write to disk; the operator is expected
to review the printed list and re-run with ``--set tags=...`` manually.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lies import xdg
from lies.cli.collections import collections_app
from lies.collections.record import Collection, load_collection, save_collection
from lies.wiki.wiki import Wiki

runner = CliRunner()


def _wiki(name: str) -> Wiki:
    """Build a Wiki whose role roots follow the (isolated) XDG env vars.

    Mirrors the construction used in ``test_collections_help.py`` so
    ``Wiki.require`` resolves it through the same path the CLI does.
    """
    return Wiki(
        name=name,
        data_root=xdg.data_home() / "lies" / name,
        config_root=xdg.config_home() / "lies" / name,
        cache_root=xdg.cache_home() / "lies" / name,
        state_root=xdg.state_home() / "lies" / name,
        runtime_root=xdg.runtime_dir_for(name),
    )


@pytest.fixture
def fake_wiki_with_empty_tags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Wiki:
    """A wiki with two collections: ``airflow`` (empty tags) and ``htmx`` (tags).

    Only ``airflow`` should appear in the dry-run hint output. Both YAMLs
    must remain unchanged after the dry-run completes.
    """
    name = "enrichtags"
    monkeypatch.setenv("LIES_WIKI_NAME", name)
    wiki = _wiki(name)
    wiki.data_root.mkdir(parents=True, exist_ok=True)
    wiki.collections_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(tz=UTC)
    save_collection(
        wiki,
        Collection(
            name="airflow",
            path=wiki.data_root / "raw" / "airflow",
            source="./raw/airflow",
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
    save_collection(
        wiki,
        Collection(
            name="htmx",
            path=wiki.data_root / "raw" / "htmx",
            source="./raw/htmx",
            tags=["python", "web"],
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
    return wiki


def test_collections_enrich_tags_dry_run(fake_wiki_with_empty_tags: Wiki) -> None:
    """``enrich-tags`` prints a hint for empty-tag collections and writes nothing."""
    result = runner.invoke(
        collections_app,
        ["enrich-tags", "--name", fake_wiki_with_empty_tags.name],
    )
    assert result.exit_code == 0, result.stdout
    # Output should contain a `lies collections modify` invocation per empty-tag collection.
    assert "lies collections modify airflow" in result.stdout
    assert "--set tags=" in result.stdout
    # htmx has tags, so it should not appear in the hint output.
    assert "lies collections modify htmx" not in result.stdout
    # Dry-run must not have written anything; the airflow.yaml is
    # still on disk and its tags remain empty.
    airflow_yaml = fake_wiki_with_empty_tags.collections_dir / "airflow.yaml"
    assert airflow_yaml.exists()
    assert load_collection(fake_wiki_with_empty_tags, "airflow").tags == []


def test_collections_enrich_tags_apply_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--apply`` is reserved for future auto-apply and currently raises."""
    name = "enrichtags-apply"
    monkeypatch.setenv("LIES_WIKI_NAME", name)
    wiki = _wiki(name)
    wiki.data_root.mkdir(parents=True, exist_ok=True)
    wiki.collections_dir.mkdir(parents=True, exist_ok=True)
    result = runner.invoke(collections_app, ["enrich-tags", "--apply", "--name", name])
    assert result.exit_code != 0
    assert "does not auto-apply" in (result.stdout + result.stderr)
