"""Tests for SyncOrchestrator state machine and StageResult."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest

from lies import xdg
from lies.collections.record import Collection
from lies.etl.cost import CostBudget
from lies.etl.errors import BudgetExceeded
from lies.etl.pipeline import PipelineState, StageResult, SyncOrchestrator
from lies.etl.telemetry import SyncTelemetry
from lies.scrapers.base import ParsedDoc
from lies.wiki.wiki import Wiki


@pytest.fixture
def wiki(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Wiki:
    """A Wiki with all five XDG roots under ``tmp_path`` so tests are hermetic."""
    monkeypatch.setenv("LIES_XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("LIES_XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("LIES_XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("LIES_XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("LIES_XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    name = "test"
    wiki = Wiki(
        name=name,
        data_root=Wiki.data_root_for(name),
        config_root=xdg.config_home() / "lies" / name,
        cache_root=xdg.cache_home() / "lies" / name,
        state_root=xdg.state_home() / "lies" / name,
        runtime_root=xdg.runtime_dir_for(name),
    )
    wiki.data_root.mkdir(parents=True, exist_ok=True)
    wiki.state_root.mkdir(parents=True, exist_ok=True)
    return wiki


def _collection(wiki: Wiki) -> Collection:
    return Collection(
        name="cpython",
        path=wiki.data_root / "raw" / "cpython",
        source="https://example.com",
        tags=[],
        scraper_cmd=None,
        doc_path=None,
        mapper_model=None,
        language=None,
        version="1.0.0",
        created_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )


def test_pipeline_runs_all_states(wiki: Wiki) -> None:
    collection = _collection(wiki)
    telemetry = SyncTelemetry(wiki, collection.name)
    budget = CostBudget()
    pipeline = SyncOrchestrator(
        collection=collection,
        telemetry=telemetry,
        budget=budget,
        wiki=wiki,
        manifest=mock.Mock(),
        
    )
    fake_docs = [
        ParsedDoc(path="x.md", content=b"# hi", source_sha256="abc", source_format="markdown")
    ]
    with (
        mock.patch(
            "lies.etl.pipeline.run_scrape",
            return_value=StageResult(
                success=["x.md"],
                quarantined=[],
                skipped=[],
                parsed_docs=fake_docs,
                bytes_in=10,
                bytes_out=0,
            ),
        ),
        mock.patch(
            "lies.etl.pipeline.run_normalize",
            return_value=StageResult(
                success=["x.md"],
                quarantined=[],
                skipped=[],
                parsed_docs=fake_docs,
                bytes_in=10,
                bytes_out=10,
            ),
        ),
        mock.patch(
            "lies.etl.pipeline.run_write",
            return_value=StageResult(
                success=["x.md"],
                quarantined=[],
                skipped=[],
                parsed_docs=[],
                bytes_in=0,
                bytes_out=10,
            ),
        ),
        mock.patch(
            "lies.etl.pipeline.run_qmd_update",
            return_value=StageResult(
                success=[], quarantined=[], skipped=[], parsed_docs=[], bytes_in=0, bytes_out=0
            ),
        ),
    ):
        pipeline.run()
    assert pipeline.state == PipelineState.IDLE


def test_pipeline_rolls_back_on_budget_exceeded(wiki: Wiki) -> None:
    collection = _collection(wiki)
    telemetry = SyncTelemetry(wiki, collection.name)
    budget = CostBudget(calls=0, tokens=10_000)
    manifest = mock.Mock()
    pipeline = SyncOrchestrator(
        collection=collection,
        telemetry=telemetry,
        budget=budget,
        wiki=wiki,
        manifest=manifest,
        
    )
    with (
        mock.patch("lies.etl.pipeline.run_scrape", side_effect=BudgetExceeded((1, 0), (0, 10_000))),
        pytest.raises(BudgetExceeded),
    ):
        pipeline.run()
    manifest.restore.assert_called_once()


def test_pipeline_threads_parsed_docs_from_scrape_to_normalize(wiki: Wiki) -> None:
    """Scrape returns parsed_docs; orchestrator passes them to normalize."""
    collection = _collection(wiki)
    telemetry = SyncTelemetry(wiki, collection.name)
    budget = CostBudget()
    pipeline = SyncOrchestrator(
        collection=collection,
        telemetry=telemetry,
        budget=budget,
        wiki=wiki,
        manifest=mock.Mock(),
        
    )
    fake_docs = [
        ParsedDoc(path="x.md", content=b"# hi", source_sha256="abc", source_format="markdown")
    ]
    captured: dict = {}

    def fake_scrape(wiki, c):
        return StageResult(
            success=["x.md"],
            quarantined=[],
            skipped=[],
            parsed_docs=fake_docs,
            bytes_in=10,
            bytes_out=0,
        )

    def fake_normalize(wiki, c, docs):
        captured["docs"] = docs
        return StageResult(
            success=["x.md"], quarantined=[], skipped=[], parsed_docs=[], bytes_in=10, bytes_out=10
        )

    def fake_write(wiki, c, normalized, *, manifest, force):
        return StageResult(
            success=[], quarantined=[], skipped=[], parsed_docs=[], bytes_in=0, bytes_out=0
        )

    def fake_qmd(wiki, c):
        return StageResult(
            success=[], quarantined=[], skipped=[], parsed_docs=[], bytes_in=0, bytes_out=0
        )

    with (
        mock.patch("lies.etl.pipeline.run_scrape", side_effect=fake_scrape),
        mock.patch("lies.etl.pipeline.run_normalize", side_effect=fake_normalize),
        mock.patch("lies.etl.pipeline.run_write", side_effect=fake_write),
        mock.patch("lies.etl.pipeline.run_qmd_update", side_effect=fake_qmd),
    ):
        pipeline.run()
    assert captured["docs"] is fake_docs


def test_pipeline_threads_force_to_write(wiki: Wiki) -> None:
    collection = _collection(wiki)
    telemetry = SyncTelemetry(wiki, collection.name)
    budget = CostBudget()
    pipeline = SyncOrchestrator(
        collection=collection,
        telemetry=telemetry,
        budget=budget,
        wiki=wiki,
        manifest=mock.Mock(),
        
        force=True,
    )
    captured: dict = {}

    def fake_scrape(wiki, c):
        return StageResult(
            success=[], quarantined=[], skipped=[], parsed_docs=[], bytes_in=0, bytes_out=0
        )

    def fake_normalize(wiki, c, docs):
        return StageResult(
            success=[], quarantined=[], skipped=[], parsed_docs=[], bytes_in=0, bytes_out=0
        )

    def fake_write(wiki, c, normalized, *, manifest, force):
        captured["force"] = force
        captured["wiki"] = wiki
        return StageResult(
            success=[], quarantined=[], skipped=[], parsed_docs=[], bytes_in=0, bytes_out=0
        )

    def fake_qmd(wiki, c):
        return StageResult(
            success=[], quarantined=[], skipped=[], parsed_docs=[], bytes_in=0, bytes_out=0
        )

    with (
        mock.patch("lies.etl.pipeline.run_scrape", side_effect=fake_scrape),
        mock.patch("lies.etl.pipeline.run_normalize", side_effect=fake_normalize),
        mock.patch("lies.etl.pipeline.run_write", side_effect=fake_write),
        mock.patch("lies.etl.pipeline.run_qmd_update", side_effect=fake_qmd),
    ):
        pipeline.run()
    assert captured["force"] is True
    assert captured["wiki"].data_root == wiki.data_root


def test_pipeline_threads_wiki_to_write(wiki: Wiki) -> None:
    """The orchestrator must thread a Wiki into the write stage."""
    collection = _collection(wiki)
    telemetry = SyncTelemetry(wiki, collection.name)
    budget = CostBudget()
    pipeline = SyncOrchestrator(
        collection=collection,
        telemetry=telemetry,
        budget=budget,
        wiki=wiki,
        manifest=mock.Mock(),
        
    )
    captured: dict = {}

    def fake_scrape(wiki, c):
        return StageResult(
            success=[], quarantined=[], skipped=[], parsed_docs=[], bytes_in=0, bytes_out=0
        )

    def fake_normalize(wiki, c, docs):
        return StageResult(
            success=[], quarantined=[], skipped=[], parsed_docs=[], bytes_in=0, bytes_out=0
        )

    def fake_write(wiki, c, normalized, *, manifest, force):
        captured["wiki_root"] = wiki.data_root
        return StageResult(
            success=[], quarantined=[], skipped=[], parsed_docs=[], bytes_in=0, bytes_out=0
        )

    def fake_qmd(wiki, c):
        return StageResult(
            success=[], quarantined=[], skipped=[], parsed_docs=[], bytes_in=0, bytes_out=0
        )

    with (
        mock.patch("lies.etl.pipeline.run_scrape", side_effect=fake_scrape),
        mock.patch("lies.etl.pipeline.run_normalize", side_effect=fake_normalize),
        mock.patch("lies.etl.pipeline.run_write", side_effect=fake_write),
        mock.patch("lies.etl.pipeline.run_qmd_update", side_effect=fake_qmd),
    ):
        pipeline.run()
    assert captured["wiki_root"] == wiki.data_root


def test_pipeline_runs_register_stage(wiki: Wiki, monkeypatch: pytest.MonkeyPatch) -> None:
    """After WRITE and before QMD_UPDATE, the pipeline calls WikiMemoryService.register_collection."""
    wiki.data_root.mkdir(parents=True, exist_ok=True)
    (wiki.data_root / "wiki").mkdir(parents=True, exist_ok=True)
    (wiki.data_root / "raw").mkdir(parents=True, exist_ok=True)
    (wiki.collections_dir).mkdir(parents=True, exist_ok=True)
    c = Collection(
        name="reg_test",
        path=wiki.data_root / "raw" / "reg_test",
        source="",
        tags=[],
        scraper_cmd=None,
        doc_path=None,
        mapper_model=None,
        language=None,
        version="1.0.0",
        created_at=datetime.now(tz=timezone.utc),
        updated_at=datetime.now(tz=timezone.utc),
        config={},
    )
    telemetry = SyncTelemetry(wiki, c.name)
    from lies.collections.hash_manifest import HashManifest

    manifest = HashManifest(wiki.data_root, c.name)
    budget = CostBudget()
    orch = SyncOrchestrator(
        collection=c,
        telemetry=telemetry,
        budget=budget,
        wiki=wiki,
        manifest=manifest,
        
    )
    # Stub each stage to keep the test focused on the new state transition.
    with (
        mock.patch("lies.etl.pipeline.run_scrape") as m_scrape,
        mock.patch("lies.etl.pipeline.run_normalize") as m_normalize,
        mock.patch("lies.etl.pipeline.run_write") as m_write,
        mock.patch("lies.etl.pipeline.run_register") as m_register,
        mock.patch("lies.etl.pipeline.run_qmd_update") as m_qmd,
    ):
        m_scrape.return_value = StageResult(success=[], quarantined=[], skipped=[], parsed_docs=[])
        m_normalize.return_value = StageResult(
            success=[], quarantined=[], skipped=[], parsed_docs=[]
        )
        m_write.return_value = StageResult(
            success=["x.md"], quarantined=[], skipped=[], parsed_docs=[]
        )
        m_register.return_value = StageResult(
            success=[], quarantined=[], skipped=[], parsed_docs=[]
        )
        orch.run()
    m_register.assert_called_once()
    args, _ = m_register.call_args
    assert args[1] is c
    assert args[2] is orch._service
    m_qmd.assert_called_once()
    qmd_args, _ = m_qmd.call_args
    assert qmd_args[1] is c
