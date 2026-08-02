"""Collection record and YAML config persistence.

A collection describes one documentation source. Configs live in
``<wiki>/.lies/collections/<name>.yaml``. The ``name`` field is the
primary key and must not contain QMD operator characters.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import yaml  # type: ignore[import-untyped]

from lies.collections.errors import (
    CollectionConfigInvalid,
    CollectionNameRejected,
    CollectionNotFound,
)

CONFIG_DIR_NAME = "collections"
_OPERATOR_CHARS_RE = re.compile(r"[+&|\-]")


@dataclass(frozen=True)
class Collection:
    """Configuration record for one documentation collection."""

    name: str
    path: Path
    source: str
    tags: list[str]
    scraper_cmd: str | None
    doc_path: Path | None
    mapper_model: str | None
    language: str | None
    version: str
    created_at: datetime
    updated_at: datetime

    def qmd_name(self) -> str:
        """Return the collection name used by QMD."""
        return self.name

    def rejects_operator_chars(self) -> None:
        """Reject names containing reserved QMD operator characters."""
        if _OPERATOR_CHARS_RE.search(self.name):
            raise CollectionNameRejected(self.name)


def _config_dir(wiki_root: Path) -> Path:
    return wiki_root / ".lies" / CONFIG_DIR_NAME


def _parse_dt(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    raise CollectionConfigInvalid(f"invalid datetime: {value!r}")


def load_collection(wiki_root: Path, name: str) -> Collection:
    """Load and validate a collection config from ``wiki_root``."""
    config_path = _config_dir(wiki_root) / f"{name}.yaml"
    if not config_path.exists():
        raise CollectionNotFound(f"collection {name!r} not found at {config_path}")

    try:
        payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise CollectionConfigInvalid(f"invalid YAML in {config_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise CollectionConfigInvalid(f"config root must be a mapping: {config_path}")

    try:
        collection = Collection(
            name=payload["name"],
            path=Path(payload["path"]),
            source=payload["source"],
            tags=list(payload.get("tags", [])),
            scraper_cmd=payload.get("scraper_cmd"),
            doc_path=Path(payload["doc_path"]) if payload.get("doc_path") else None,
            mapper_model=payload.get("mapper_model"),
            language=payload.get("language"),
            version=payload["version"],
            created_at=_parse_dt(payload["created_at"]),
            updated_at=_parse_dt(payload["updated_at"]),
        )
    except KeyError as exc:
        raise CollectionConfigInvalid(f"missing field {exc} in {config_path}") from exc

    collection.rejects_operator_chars()
    return collection


def save_collection(
    wiki_root: Path,
    collection: Collection,
    *,
    in_memory_only: bool = False,
) -> None:
    """Validate and save a collection config under ``wiki_root``."""
    collection.rejects_operator_chars()
    payload = asdict(collection)
    payload["path"] = str(collection.path)
    payload["doc_path"] = str(collection.doc_path) if collection.doc_path else None
    payload["created_at"] = collection.created_at.isoformat()
    payload["updated_at"] = collection.updated_at.isoformat()
    if in_memory_only:
        return

    config_path = _config_dir(wiki_root) / f"{collection.name}.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.safe_dump(payload, sort_keys=True), encoding="utf-8")
