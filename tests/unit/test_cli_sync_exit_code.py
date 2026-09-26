"""Tests for ``lies sync`` exit-code propagation from ``BatchIngestResult.errors``.

Finding 3 pin: a wholly-failed sync (errors > 0) must exit non-zero
so operators (and CI) notice the silent data loss. The previous
behavior swallowed ``BatchIngestResult`` and exited 0 regardless.

Sync auto-reindex chain (sync-auto-reindex-embed):
    ``lies sync`` (no ``--skip-reindex``) chains ``qmd_reindex`` against
    ``library_git_root()`` after the collection loop so operators no
    longer need a separate ``/reindex`` invocation. CI matrices that
    reindex separately pass ``--skip-reindex``. The 3 historical
    exit-code tests below pass ``--skip-reindex`` so they stay focused
    on the exit-code contract; the new chain-contract tests pin the
    auto-chain shape end-to-end.

Marked slow: each test invokes the real Typer CLI (~300ms per
``runner.invoke``) so the unit budget does not absorb the inherent
CLI-machinery cost. Run with ``--runslow`` to exercise.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from lies import xdg
from lies.cli import app
from lies.library.ingest import BatchIngestResult
from lies.library.paths import Library
from lies.wiki.wiki import Wiki

pytestmark = pytest.mark.slow

runner = CliRunner()


@pytest.fixture
def wiki_with_collection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Wiki:
    """Wiki + library + one collection config, fully hermetic under tmp_path."""
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
    # Library under the same XDG_DATA_HOME; the helper opens it via
    # ``Library.open()`` which honours the env override.
    lib = Library.open()
    lib.git_root.mkdir(parents=True, exist_ok=True)
    # ``sync_helper.collection_names`` (Task 4) reads from the library
    # registry, not the wiki YAML dir. Seed the library-side config so
    # the CLI's ``lies sync`` (multi-collection mode) discovers ``alpha``.
    (lib.collections_root / "alpha").mkdir(parents=True, exist_ok=True)
    (lib.collections_root / "alpha" / "config.yaml").write_text(
        "name: alpha\n"
        "source: https://example.com/alpha\n"
        "tags: []\n"
        "version: '1'\n"
        "created_at: 2026-01-01T00:00:00\n"
        "updated_at: 2026-01-01T00:00:00\n",
        encoding="utf-8",
    )
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
    result = runner.invoke(app, ["sync", "alpha", "--skip-reindex"])
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
    result = runner.invoke(app, ["sync", "alpha", "--skip-reindex"])
    assert result.exit_code == 1, result.output
    out = result.output
    assert "errors=2" in out


def test_sync_aggregates_errors_across_collections(
    wiki_with_collection: Wiki, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-collection errors aggregate across the multi-collection run."""
    # Seed a second collection in the library registry (Task 4 reads
    # multi-collection mode from the library, not the wiki YAML dir).
    lib = Library.open()
    (lib.collections_root / "beta").mkdir(parents=True, exist_ok=True)
    (lib.collections_root / "beta" / "config.yaml").write_text(
        "name: beta\n"
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
    result = runner.invoke(app, ["sync", "--skip-reindex"])
    assert result.exit_code == 1, result.output
    out = result.output
    # Total errors = 1 + 2 = 3.
    assert "errors=3" in out
    assert "created=3" in out


def test_sync_chains_qmd_reindex_by_default(
    wiki_with_collection: Wiki, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default ``lies sync`` chains ``qmd_reindex`` after the collection loop.

    Pin the auto-chain contract: ``lies sync alpha`` without
    ``--skip-reindex`` MUST invoke ``qmd_reindex`` exactly once
    against ``library_git_root()`` with ``embed=True``. The qmd
    failure mode does NOT abort the sync exit code — qmd lag is
    operator-visible, not data-loss (the brief is explicit on this).
    """
    from lies.etl import sync_helper
    from lies.qmd import _models, cli as qmd_cli

    monkeypatch.setattr(
        sync_helper,
        "sync_collection",
        lambda wiki, coll_name, *, force: BatchIngestResult(created=3, errors=0),
    )
    with patch.object(
        qmd_cli,
        "qmd_reindex",
        return_value=_models.ReindexResult(indexed=True, embedded=True),
    ) as mock:
        result = runner.invoke(app, ["sync", "alpha"])
    assert result.exit_code == 0, result.output
    mock.assert_called_once()
    # First positional arg is the cwd; the chain passes library_git_root().
    args = mock.call_args.args
    assert len(args) == 1
    assert Path(args[0]) == Path(Library.open().git_root)
    # embed=True so the chain re-embeds stale chunks alongside the BM25 update.
    kwargs = mock.call_args.kwargs
    assert kwargs.get("embed") is True


def test_sync_skip_reindex_skips_qmd_chain(
    wiki_with_collection: Wiki, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--skip-reindex`` opts out of the qmd chain entirely.

    Pin the opt-out contract for CI matrices that reindex separately.
    """
    from lies.etl import sync_helper
    from lies.qmd import _models, cli as qmd_cli

    monkeypatch.setattr(
        sync_helper,
        "sync_collection",
        lambda wiki, coll_name, *, force: BatchIngestResult(created=3, errors=0),
    )
    with patch.object(
        qmd_cli,
        "qmd_reindex",
        return_value=_models.ReindexResult(),
    ) as mock:
        result = runner.invoke(app, ["sync", "alpha", "--skip-reindex"])
    assert result.exit_code == 0, result.output
    mock.assert_not_called()


def test_sync_qmd_reindex_failure_warns_but_exits_zero(
    wiki_with_collection: Wiki, monkeypatch: pytest.MonkeyPatch
) -> None:
    """qmd_reindex failure logs a warning; sync exit code stays clean.

    Pin the ``try/except Exception`` envelope: a qmd reindex wedge
    is operator-visible (log + warning) but does NOT abort the sync
    exit code, because sync data integrity is separate from qmd
    lag. The collection sync already exited 0; qmd lag is a
    follow-up concern.
    """
    from lies.etl import sync_helper
    from lies.qmd import cli as qmd_cli

    monkeypatch.setattr(
        sync_helper,
        "sync_collection",
        lambda wiki, coll_name, *, force: BatchIngestResult(created=1, errors=0),
    )
    with patch.object(
        qmd_cli,
        "qmd_reindex",
        side_effect=RuntimeError("simulated qmd wedge"),
    ):
        result = runner.invoke(app, ["sync", "alpha"])
    assert result.exit_code == 0, result.output
    assert "warning: qmd reindex failed" in result.output
