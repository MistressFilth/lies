import json
from pathlib import Path

import pytest

from lies.etl.telemetry import SyncTelemetry


def test_telemetry_records_events(tmp_path: Path) -> None:
    t = SyncTelemetry("cpython", log_dir=tmp_path)
    t.record_stage("scraping", docs=5, bytes_in=1024)
    t.record_stage("normalizing", docs=4, bytes_in=0, quarantined=1)
    log = tmp_path / "cpython.log"
    assert log.exists()
    events = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert events[0]["stage"] == "scraping"
    assert events[1]["quarantined"] == 1


def test_receipt_aggregates_counts(tmp_path: Path) -> None:
    t = SyncTelemetry("cpython", log_dir=tmp_path)
    t.record_counters(
        docs_total=10,
        docs_ingested=4,
        docs_skipped=3,
        docs_quarantined=1,
        bytes_in=2000,
        bytes_out=1500,
    )
    t.record_counters(qmd_index_time_ms=120, model_calls=2, model_tokens=300)
    t.record_started("2026-08-01T00:00:00Z")
    t.record_ended("2026-08-01T00:01:00Z")
    r = t.receipt()
    assert r.docs_total == 10
    assert r.model_calls == 2
    assert r.bytes_out == 1500


def test_receipt_default_zeros(tmp_path: Path) -> None:
    t = SyncTelemetry("cpython", log_dir=tmp_path)
    r = t.receipt()
    assert r.docs_ingested == 0
    assert r.collection == "cpython"


def test_record_error_appends_to_receipt(tmp_path: Path) -> None:
    t = SyncTelemetry("cpython", log_dir=tmp_path)
    t.record_error("budget_exceeded")
    t.record_error("qmd_missing")
    r = t.receipt()
    assert "budget_exceeded" in r.errors
    assert "qmd_missing" in r.errors


def test_record_counters_rejects_unknown_counter(tmp_path: Path) -> None:
    t = SyncTelemetry("cpython", log_dir=tmp_path)
    with pytest.raises(ValueError, match="unknown counter"):
        t.record_counter("bogus", 1)


def test_record_counter_accumulates_in_place(tmp_path: Path) -> None:
    """``record_counter`` is additive across stages.

    The pipeline runs through SCRAPE → NORMALIZE → WRITE; each stage may
    see ``bytes_in`` independently. The receipt must reflect the total
    seen across stages, not the value of the last call.
    """
    t = SyncTelemetry("cpython", log_dir=tmp_path)
    t.record_counter("bytes_in", 100)
    t.record_counter("bytes_in", 250)
    t.record_counter("docs_total", 5)
    r = t.receipt()
    assert r.bytes_in == 350
    assert r.docs_total == 5
    events = [json.loads(line) for line in (tmp_path / "cpython.log").read_text().splitlines()]
    # Two counter events were recorded for bytes_in; the per-call value
    # is what landed, not the cumulative total.
    bytes_in_events = [e for e in events if e["kind"] == "counters" and e["name"] == "bytes_in"]
    assert [e["delta"] for e in bytes_in_events] == [100, 250]


def test_context_manager_closes_on_exception(tmp_path: Path) -> None:
    """`with SyncTelemetry(...) as t:` closes the file even on raise."""
    with pytest.raises(RuntimeError, match="boom"), SyncTelemetry("cpython", log_dir=tmp_path) as t:
        t.record_stage("scraping")
        raise RuntimeError("boom")
    # File handle is closed; re-opening in append mode works.
    with SyncTelemetry("cpython", log_dir=tmp_path) as t2:
        t2.record_stage("normalizing")
    log = (tmp_path / "cpython.log").read_text(encoding="utf-8")
    assert "scraping" in log
    assert "normalizing" in log


def test_context_manager_returns_self(tmp_path: Path) -> None:
    """`with SyncTelemetry(...) as t:` returns the telemetry instance."""
    with SyncTelemetry("cpython", log_dir=tmp_path) as t:
        assert isinstance(t, SyncTelemetry)
