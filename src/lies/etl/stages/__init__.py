"""Pipeline stage modules — implementations land in Tasks 21-24.

Each stage takes the ``Collection`` (and any thread-through data) and
returns a ``StageResult``. The real stage implementations import
``StageResult`` from ``lies.etl.pipeline``; this package only ships
the stubs so the orchestrator can be loaded and tested in Task 20.
"""
from __future__ import annotations

__all__ = ["run_normalize", "run_qmd_update", "run_scrape", "run_write"]