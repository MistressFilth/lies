import json
from pathlib import Path

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
    t.record_counters(docs_total=10, docs_ingested=4, docs_skipped=3,
                      docs_quarantined=1, bytes_in=2000, bytes_out=1500)
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