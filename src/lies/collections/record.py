"""Collection record and YAML config persistence.

Configs live in the wiki's XDG config directory. The ``name`` field is the
primary key and must not contain QMD operator characters.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml  # type: ignore[import-untyped]

from lies.collections.errors import (
    CollectionConfigInvalid,
    CollectionNameRejected,
    CollectionNotFound,
)

if TYPE_CHECKING:
    from lies.wiki.wiki import Wiki

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
    config: dict[str, Any] = field(default_factory=dict)

    def qmd_name(self) -> str:
        """Return the collection name used by QMD."""
        return self.name

    def rejects_operator_chars(self) -> None:
        """Reject names containing reserved QMD operator characters."""
        if _OPERATOR_CHARS_RE.search(self.name):
            raise CollectionNameRejected(self.name)

    @staticmethod
    def config_path(wiki: Wiki, name: str) -> Path:
        """Return the on-disk YAML path for a collection under ``wiki``."""
        return wiki.collections_dir / f"{name}.yaml"


def _parse_dt(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    raise CollectionConfigInvalid(f"invalid datetime: {value!r}")


def load_collection(wiki: Wiki, name: str) -> Collection:
    """Load and validate a collection config from ``wiki``."""
    from lies.wiki.wiki import Wiki

    if not isinstance(wiki, Wiki):
        raise TypeError("load_collection requires a Wiki instance")
    config_path = Collection.config_path(wiki, name)
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
            config=payload.get("config") or {},
        )
    except KeyError as exc:
        raise CollectionConfigInvalid(f"missing field {exc} in {config_path}") from exc

    collection.rejects_operator_chars()
    return collection


def save_collection(
    wiki: Wiki,
    collection: Collection,
    *,
    in_memory_only: bool = False,
) -> None:
    """Validate and save a collection config under ``wiki``.

    Writes atomically: payload is dumped to ``<config_path>.tmp`` next
    to the target, fsync'd, then ``os.replace``d into place. On any
    ``OSError`` the tmp file is removed and ``CollectionWriteFailed``
    is raised. With ``in_memory_only=True``, no IO occurs (used by
    callers that only want validation + the dataclass-shaped payload).
    """
    from lies.wiki.wiki import Wiki

    if not isinstance(wiki, Wiki):
        raise TypeError("save_collection requires a Wiki instance")
    collection.rejects_operator_chars()
    payload = asdict(collection)
    payload["path"] = str(collection.path)
    payload["doc_path"] = str(collection.doc_path) if collection.doc_path else None
    payload["created_at"] = collection.created_at.isoformat()
    payload["updated_at"] = collection.updated_at.isoformat()
    if in_memory_only:
        return

    config_path = Collection.config_path(wiki, collection.name)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = config_path.with_suffix(config_path.suffix + ".tmp")
    import contextlib
    import os

    from lies.collections.errors import CollectionWriteFailed

    try:
        with tmp.open("w", encoding="utf-8") as fh:
            yaml.safe_dump(payload, fh, sort_keys=True)
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
