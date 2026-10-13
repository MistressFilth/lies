"""Pydantic schema for ``<library>/collections/<slug>/config.yaml``."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field


class ConfigYAML(BaseModel):
    """Canonical schema for a library collection's ``config.yaml``."""

    model_config = ConfigDict(extra="ignore", populate_by_name=False)

    name: str
    source: str
    description: str | None = None
    tags: tuple[str, ...] = ()
    scope_keywords: tuple[str, ...] = ()
    scraper_cmd: str | None = None
    doc_path: Path | None = None
    mapper_model: str | None = None
    language: str | None = None
    version: str = "1"
    created_at: datetime
    updated_at: datetime
    config: dict[str, Any] = Field(default_factory=dict)


def dump_config_yaml(config: ConfigYAML) -> str:
    """Serialize ``config`` to YAML for atomic disk write."""
    payload = config.model_dump(mode="json")
    return yaml.safe_dump(payload, sort_keys=True)
