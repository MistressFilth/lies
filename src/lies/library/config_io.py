"""Library collection config load/save.

Configs live at ``<library>/collections/<slug>/config.yaml``. Loads
require the file to exist; saves are atomic (write to ``.tmp``,
fsync, ``os.replace``). ``force=True`` overwrites an existing target;
without ``force`` a content mismatch raises :class:`CollectionAlreadyExists`.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

import yaml  # type: ignore[import-untyped]

from lies.library.errors import (
    CollectionAlreadyExists,
    CollectionConfigInvalid,
    CollectionNotFound,
    CollectionWriteFailed,
)
from lies.library.paths import Library
from lies.library.record import LibraryCollectionConfig
from lies.library.schema import ConfigYAML


def config_path_for(slug: str) -> Path:
    """Return the on-disk config path for ``slug``."""
    return Library.open().collections_root / slug / "config.yaml"


def load_config(slug: str) -> LibraryCollectionConfig:
    """Load and validate the library collection config for ``slug``."""
    config_path = config_path_for(slug)
    if not config_path.exists():
        raise CollectionNotFound(f"collection {slug!r} not found at {config_path}")
    try:
        payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise CollectionConfigInvalid(f"invalid YAML in {config_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise CollectionConfigInvalid(f"config root must be a mapping: {config_path}")
    # Drop empty `config` (None or {}) to satisfy schema default.
    if not payload.get("config"):
        payload["config"] = {}
    try:
        schema = ConfigYAML.model_validate(payload)
    except Exception as exc:
        raise CollectionConfigInvalid(f"invalid config at {config_path}: {exc}") from exc
    return _schema_to_record(schema)


def save_config(config: LibraryCollectionConfig, *, force: bool = False) -> None:
    """Atomically write ``config`` to its library location."""
    config_path = config_path_for(config.name)
    if config_path.exists() and not force:
        raise CollectionAlreadyExists(
            f"config already exists at {config_path}; pass force=True to overwrite"
        )
    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = config_path.with_suffix(config_path.suffix + ".tmp")
    from lies.library.schema import dump_config_yaml

    schema = _record_to_schema(config)
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(dump_config_yaml(schema))
            fh.flush()
            if hasattr(os, "fsync"):
                os.fsync(fh.fileno())
        os.replace(tmp, config_path)
    except BaseException as exc:
        with contextlib.suppress(OSError):
            tmp.unlink()
        if isinstance(exc, OSError):
            raise CollectionWriteFailed(config_path, str(exc)) from exc
        raise


def _schema_to_record(schema: ConfigYAML) -> LibraryCollectionConfig:
    return LibraryCollectionConfig(
        name=schema.name,
        source=schema.source,
        tags=schema.tags,
        scraper_cmd=schema.scraper_cmd,
        doc_path=schema.doc_path,
        mapper_model=schema.mapper_model,
        language=schema.language,
        version=schema.version,
        created_at=schema.created_at,
        updated_at=schema.updated_at,
        config=dict(schema.config),
    )


def _record_to_schema(record: LibraryCollectionConfig) -> ConfigYAML:
    return ConfigYAML(
        name=record.name,
        source=record.source,
        tags=record.tags,
        scraper_cmd=record.scraper_cmd,
        doc_path=record.doc_path,
        mapper_model=record.mapper_model,
        language=record.language,
        version=record.version,
        created_at=record.created_at,
        updated_at=record.updated_at,
        config=dict(record.config),
    )
