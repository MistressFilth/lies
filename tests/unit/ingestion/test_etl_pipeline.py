"""Tests for SyncOrchestrator state machine and StageResult."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest

from lies.collections.record import Collection
from lies.etl.cost import CostBudget
from lies.etl.errors import BudgetExceeded
from lies.etl.pipeline import PipelineState, StageResult, SyncOrchestrator
from lies.etl.telemetry import SyncTelemetry
from lies.scrapers.base import ParsedDoc


def _collection(tmp_path: Path) -> Collection:
    return Collection(
        name="cpython",
        path=tmp_path / "raw" / "cpython",
        source="https://example.com",
        tags=[], scraper_cmd=None, doc_path=None,
        mapper_model=None, language=None, version="1.0.0",
        created_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )


def test_pipeline_runs_all_states(tmp_path: Path) -> None:
    collection = _collection(tmp_path)
    telemetry = SyncTelemetry(collection.name, tmp_path / "logs")
    budget = CostBudget()
    pipeline = SyncOrchestrator(
        collection=collection, telemetry=telemetry, budget=budget,
        manifest=mock.Mock(), wiki_root=tmp_path,
    )
    fake_docs = [ParsedDoc(path="x.md", content=b"# hi", source_sha256="abc", source_format="markdown")]
    with mock.patch("lies.etl.pipeline.run_scrape", return_value=StageResult(
            success=["x.md"], quarantined=[], skipped=[],
            parsed_docs=fake_docs, bytes_in=10, bytes_out=0)), \
         mock.patch("lies.etl.pipeline.run_normalize", return_value=StageResult(
             success=["x.md"], quarantined=[], skipped=[],
             parsed_docs=fake_docs, bytes_in=10, bytes_out=10)), \
         mock.patch("lies.etl.pipeline.run_write", return_value=StageResult(
             success=["x.md"], quarantined=[], skipped=[],
             parsed_docs=[], bytes_in=0, bytes_out=10)), \
         mock.patch("lies.etl.pipeline.run_qmd_update", return_value=StageResult(
             success=[], quarantined=[], skipped=[],
             parsed_docs=[], bytes_in=0, bytes_out=0)):
        pipeline.run()
    assert pipeline.state == PipelineState.IDLE


def test_pipeline_rolls_back_on_budget_exceeded(tmp_path: Path) -> None:
    collection = _collection(tmp_path)
    telemetry = SyncTelemetry(collection.name, tmp_path / "logs")
    budget = CostBudget(calls=0, tokens=10_000)
    manifest = mock.Mock()
    pipeline = SyncOrchestrator(
        collection=collection, telemetry=telemetry, budget=budget,
        manifest=manifest, wiki_root=tmp_path,
    )
    with mock.patch("lies.etl.pipeline.run_scrape", side_effect=BudgetExceeded((1, 0), (0, 10_000))), \
         pytest.raises(BudgetExceeded):
        pipeline.run()
    manifest.restore.assert_called_once()


def test_pipeline_threads_parsed_docs_from_scrape_to_normalize(tmp_path: Path) -> None:
    """Scrape returns parsed_docs; orchestrator passes them to normalize."""
    collection = _collection(tmp_path)
    telemetry = SyncTelemetry(collection.name, tmp_path / "logs")
    budget = CostBudget()
    pipeline = SyncOrchestrator(
        collection=collection, telemetry=telemetry, budget=budget,
        manifest=mock.Mock(), wiki_root=tmp_path,
    )
    fake_docs = [ParsedDoc(path="x.md", content=b"# hi", source_sha256="abc", source_format="markdown")]
    captured: dict = {}

    def fake_scrape(c):
        return StageResult(success=["x.md"], quarantined=[], skipped=[],
                          parsed_docs=fake_docs, bytes_in=10, bytes_out=0)

    def fake_normalize(c, docs):
        captured["docs"] = docs
        return StageResult(success=["x.md"], quarantined=[], skipped=[],
                           parsed_docs=[], bytes_in=10, bytes_out=10)

    def fake_write(c, normalized, *, manifest, force):
        return StageResult(success=[], quarantined=[], skipped=[],
                          parsed_docs=[], bytes_in=0, bytes_out=0)

    def fake_qmd(c):
        return StageResult(success=[], quarantined=[], skipped=[],
                          parsed_docs=[], bytes_in=0, bytes_out=0)

    with mock.patch("lies.etl.pipeline.run_scrape", side_effect=fake_scrape), \
         mock.patch("lies.etl.pipeline.run_normalize", side_effect=fake_normalize), \
         mock.patch("lies.etl.pipeline.run_write", side_effect=fake_write), \
         mock.patch("lies.etl.pipeline.run_qmd_update", side_effect=fake_qmd):
        pipeline.run()
    assert captured["docs"] is fake_docs


def test_pipeline_threads_force_to_write(tmp_path: Path) -> None:
    collection = _collection(tmp_path)
    telemetry = SyncTelemetry(collection.name, tmp_path / "logs")
    budget = CostBudget()
    pipeline = SyncOrchestrator(
        collection=collection, telemetry=telemetry, budget=budget,
        manifest=mock.Mock(), wiki_root=tmp_path, force=True,
    )
    captured: dict = {}

    def fake_scrape(c):
        return StageResult(success=[], quarantined=[], skipped=[],
                          parsed_docs=[], bytes_in=0, bytes_out=0)

    def fake_normalize(c, docs):
        return StageResult(success=[], quarantined=[], skipped=[],
                           parsed_docs=[], bytes_in=0, bytes_out=0)

    def fake_write(c, normalized, *, manifest, force):
        captured["force"] = force
        return StageResult(success=[], quarantined=[], skipped=[],
                          parsed_docs=[], bytes_in=0, bytes_out=0)

    def fake_qmd(c):
        return StageResult(success=[], quarantined=[], skipped=[],
                          parsed_docs=[], bytes_in=0, bytes_out=0)

    with mock.patch("lies.etl.pipeline.run_scrape", side_effect=fake_scrape), \
         mock.patch("lies.etl.pipeline.run_normalize", side_effect=fake_normalize), \
         mock.patch("lies.etl.pipeline.run_write", side_effect=fake_write), \
         mock.patch("lies.etl.pipeline.run_qmd_update", side_effect=fake_qmd):
        pipeline.run()
    assert captured["force"] is True