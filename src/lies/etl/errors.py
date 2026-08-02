"""Typed exceptions for the etl package."""

from __future__ import annotations

from pathlib import Path


class PipelineError(Exception):
    """Base class for pipeline-level errors."""


class BudgetExceeded(PipelineError):
    """Model-call or token budget exhausted mid-pipeline."""

    def __init__(self, spent: tuple[int, int], cap: tuple[int, int]) -> None:
        super().__init__(f"budget exceeded: spent={spent} cap={cap}")
        self.spent = spent
        self.cap = cap


class NormalizeError(PipelineError):
    """Per-doc normalization failed (quarantine candidate)."""


class WriteError(PipelineError):
    """Per-doc wiki write failed (quarantine candidate)."""


class QmdStale(PipelineError):
    """Wiki committed but qmd derived index refresh failed (informational)."""


class AtomicCommitFailed(PipelineError):
    """atomic_commit step failed and working tree was rolled back."""


class SyncBusy(PipelineError):
    """Another sync is already running on this wiki."""

    def __init__(self, holding_pid: int, wiki_root: Path | None) -> None:
        super().__init__(f"sync busy: pid={holding_pid}")
        self.holding_pid = holding_pid
        self.wiki_root = wiki_root
