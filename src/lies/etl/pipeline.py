"""SyncOrchestrator state machine + StageResult dataclass.

StageResult carries ``parsed_docs: list[ParsedDoc]`` so the
NORMALIZING stage receives the actual document objects the SCRAPE
stage produced (paths alone are insufficient for normalization).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from lies.collections.hash_manifest import HashManifest
from lies.collections.record import Collection
from lies.etl.cost import CostBudget
from lies.etl.errors import BudgetExceeded
from lies.etl.stages.normalize import run_normalize
from lies.etl.stages.qmd_update import run_qmd_update
from lies.etl.stages.scrape import run_scrape
from lies.etl.stages.write import run_write
from lies.etl.telemetry import SyncTelemetry

if TYPE_CHECKING:
    from lies.scrapers.base import ParsedDoc


class PipelineState(str, Enum):
    IDLE = "idle"
    SCRAPING = "scraping"
    NORMALIZING = "normalizing"
    WRITING = "writing"
    QMD_UPDATE = "qmd_update"
    FAILED = "failed"


@dataclass
class StageResult:
    success: list[str]
    quarantined: list[tuple[str, str]]
    skipped: list[str]
    parsed_docs: list[ParsedDoc] = field(default_factory=list)
    bytes_in: int = 0
    bytes_out: int = 0


class SyncOrchestrator:
    def __init__(
        self,
        *,
        collection: Collection,
        telemetry: SyncTelemetry,
        budget: CostBudget,
        manifest: HashManifest,
        wiki_root: Path,
        force: bool = False,
    ) -> None:
        self.collection = collection
        self.telemetry = telemetry
        self.budget = budget
        self.manifest = manifest
        self.wiki_root = wiki_root
        self.force = force
        self.state = PipelineState.IDLE

    def _transition(self, target: PipelineState) -> None:
        self.telemetry.record_stage(self.state.value, next_state=target.value)
        self.state = target

    def run(self) -> None:
        try:
            self.telemetry.record_started(datetime.now(tz=timezone.utc).isoformat())
            self._transition(PipelineState.SCRAPING)
            scraped = run_scrape(self.collection)
            self.telemetry.record_counter("docs_total", len(scraped.success))
            self.telemetry.record_counter("bytes_in", scraped.bytes_in)

            self._transition(PipelineState.NORMALIZING)
            normalized = run_normalize(self.collection, scraped.parsed_docs)
            self.telemetry.record_counter("docs_quarantined", len(normalized.quarantined))
            self.telemetry.record_counter("bytes_in", normalized.bytes_in)

            self._transition(PipelineState.WRITING)
            normalized_pairs = [
                (d.path, d.content.decode("utf-8", errors="replace"))
                for d in normalized.parsed_docs
            ]
            written = run_write(
                self.collection,
                normalized_pairs,
                manifest=self.manifest,
                force=self.force,
                wiki_root=self.wiki_root,
            )
            self.telemetry.record_counter(
                "bytes_out", written.bytes_out or normalized.bytes_out
            )

            self._transition(PipelineState.QMD_UPDATE)
            run_qmd_update(self.collection)

            self._transition(PipelineState.IDLE)
            self.telemetry.record_ended(datetime.now(tz=timezone.utc).isoformat())
        except BudgetExceeded:
            try:
                snap = self.manifest.snapshot()
                self.manifest.restore(snap)
            except Exception:  # noqa: BLE001, S110 - cleanup is best-effort
                pass
            self.telemetry.record_error("budget_exceeded")
            self.telemetry.record_ended(datetime.now(tz=timezone.utc).isoformat())
            raise
        except Exception as exc:
            self._transition(PipelineState.FAILED)
            self.telemetry.record_error(str(exc))
            self.telemetry.record_ended(datetime.now(tz=timezone.utc).isoformat())
            raise
