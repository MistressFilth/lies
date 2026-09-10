"""Tests for ``lies sync`` exit-code propagation from ``BatchIngestResult.errors``.

Finding 3 pin: a wholly-failed sync (errors > 0) must exit non-zero
so operators (and CI) notice the silent data loss. The previous
behavior swallowed ``BatchIngestResult`` and exited 0 regardless.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from lies import xdg
from lies.cli import app
from lies.library.ingest import BatchIngestResult
from lies.library.paths import Library
from lies.wiki.wiki import Wiki

runner = CliRunner()


@pytest.fixture
def wiki_with_collection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Wiki:
    """Wiki + library + one collection YAML, fully hermetic under tmp_path."""
    name = "synccli"
    monkeypatch.setenv("LIES_WIKI_NAME", name)
    monkeypatch.setenv("LIES_XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("LIES_XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("LIES_XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("LIES_XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("LIES_XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    wiki = Wiki(
        name=name,
        data_root=xdg.data_home() / "lies" / name,
        config_root=xdg.config_home() / "lies" / name,
        cache_root=xdg.cache_home() / "lies" / name,
        state_root=xdg.state_home() / "lies" / name,
        runtime_root=xdg.runtime_dir_for(name),
    )
    wiki.collections_dir.mkdir(parents=True, exist_ok=True)
    (wiki.collections_dir / "alpha.yaml").write_text(
        "name: alpha\n"
        "path: raw/alpha\n"
        "source: https://example.com/alpha\n"
        "tags: []\n"
        "version: '1'\n"
        "created_at: 2026-01-01T00:00:00\n"
        "updated_at: 2026-01-01T00:00:00\n",
        encoding="utf-8",
    )
    # Library under the same XDG_DATA_HOME; the helper opens it via
    # ``Library.open()`` which honours the env override.
    lib = Library.open()
    lib.git_root.mkdir(parents=True, exist_ok=True)
    return wiki


def test_sync_exits_zero_on_clean_batch(
    wiki_with_collection: Wiki, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A zero-error batch prints the summary and exits 0."""
    from lies.etl import sync_helper

    monkeypatch.setattr(
        sync_helper,
        "sync_collection",
        lambda wiki, coll_name, *, force: BatchIngestResult(created=3, errors=0),
    )
    result = runner.invoke(app, ["sync", "alpha"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "created=3" in out
    assert "errors=0" in out


def test_sync_exits_one_on_failed_batch(
    wiki_with_collection: Wiki, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-zero ``errors`` count exits 1; summary still printed.

    Pin for Finding 3: ``lies sync`` must NOT silently exit 0 on a
    wholly-failed batch.
    """
    from lies.etl import sync_helper

    monkeypatch.setattr(
        sync_helper,
        "sync_collection",
        lambda wiki, coll_name, *, force: BatchIngestResult(
            errors=2,
            quarantine_records=[("x:u", "fetch-unreachable:foo:UnknownFormatError")],
        ),
    )
    result = runner.invoke(app, ["sync", "alpha"])
    assert result.exit_code == 1, result.output
    out = result.output
    assert "errors=2" in out


def test_sync_aggregates_errors_across_collections(
    wiki_with_collection: Wiki, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-collection errors aggregate across the multi-collection run."""
    # Seed a second collection YAML.
    (wiki_with_collection.collections_dir / "beta.yaml").write_text(
        "name: beta\n"
        "path: raw/beta\n"
        "source: https://example.com/beta\n"
        "tags: []\n"
        "version: '1'\n"
        "created_at: 2026-01-01T00:00:00\n"
        "updated_at: 2026-01-01T00:00:00\n",
        encoding="utf-8",
    )

    from lies.etl import sync_helper

    def fake(wiki, coll_name, *, force):
        if coll_name == "alpha":
            return BatchIngestResult(created=1, errors=1)
        return BatchIngestResult(created=2, errors=2)

    monkeypatch.setattr(sync_helper, "sync_collection", fake)
    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 1, result.output
    out = result.output
    # Total errors = 1 + 2 = 3.
    assert "errors=3" in out
    assert "created=3" in out
