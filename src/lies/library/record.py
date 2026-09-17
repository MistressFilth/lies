"""Library collection config record (replaces wiki-yaml config record).

The record is the canonical shape of a library collection's metadata,
held at ``<library>/collections/<slug>/config.yaml``. The ``path``
field that the legacy ``lies.collections.record.Collection`` carried
pointed at a per-wiki raw dir that never existed on disk; library
collections hold their content directly under ``<library>/collections/<slug>/``,
and the record no longer needs to name an alternate location.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class LibraryCollectionConfig:
    """Configuration record for one library collection."""

    name: str
    source: str
    tags: tuple[str, ...] = ()
    scraper_cmd: str | None = None
    doc_path: Path | None = None
    mapper_model: str | None = None
    language: str | None = None
    version: str = "1"
    # Required fields after the defaulted ones: mark kw_only so callers
    # pass them by name (matches how all in-tree callers use it).
    created_at: datetime = field(kw_only=True)
    updated_at: datetime = field(kw_only=True)
    config: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Validate name against the library's regex. Imports the
        # validator lazily so this module's import cost stays low.
        from lies.library.paths import _validate_collection_name

        _validate_collection_name(self.name)
