"""Per-sync telemetry — NDJSON log + typed recorders + receipt aggregation."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from typing_extensions import Self


@dataclass(frozen=True)
class SyncReceipt:
    collection: str
    started_at: datetime | None
    ended_at: datetime | None
    docs_total: int
    docs_ingested: int
    docs_skipped: int
    docs_quarantined: int
    bytes_in: int
    bytes_out: int
    qmd_index_time_ms: int
    model_calls: int
    model_tokens: int
    errors: list[str] = field(default_factory=list)


class SyncTelemetry:
    def __init__(self, collection: str, log_dir: Path) -> None:
        self._collection = collection
        self._log_path = log_dir / f"{collection}.log"
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self._log_path.open("a", encoding="utf-8")
        self._counts: dict[str, int] = {
            "docs_total": 0,
            "docs_ingested": 0,
            "docs_skipped": 0,
            "docs_quarantined": 0,
            "bytes_in": 0,
            "bytes_out": 0,
            "qmd_index_time_ms": 0,
            "model_calls": 0,
            "model_tokens": 0,
        }
        self._started_at: datetime | None = None
        self._ended_at: datetime | None = None
        self._errors: list[str] = []

    def _write(self, event: dict[str, object]) -> None:
        self._fh.write(json.dumps(event) + "\n")
        self._fh.flush()

    def record_stage(self, stage: str, **extras: object) -> None:
        self._write({"collection": self._collection, "kind": "stage", "stage": stage, **extras})

    def record_counters(self, **fields: int) -> None:
        for key, val in fields.items():
            if key not in self._counts:
                raise ValueError(f"unknown counter: {key}")
            self._counts[key] = int(val)
        self._write({"collection": self._collection, "kind": "counters", **fields})

    def record_started(self, iso_ts: str) -> None:
        self._started_at = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        self._write({"collection": self._collection, "kind": "started", "ts": iso_ts})

    def record_ended(self, iso_ts: str) -> None:
        self._ended_at = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        self._write({"collection": self._collection, "kind": "ended", "ts": iso_ts})

    def record_error(self, message: str) -> None:
        self._errors.append(message)
        self._write({"collection": self._collection, "kind": "error", "message": message})

    def receipt(self) -> SyncReceipt:
        return SyncReceipt(
            collection=self._collection,
            started_at=self._started_at,
            ended_at=self._ended_at,
            errors=list(self._errors),
            **self._counts,
        )

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()
