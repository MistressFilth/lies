"""Scraper manifest — file list + content hashes from the scraper.

Stored at ``<wiki>/raw/<collection>/manifest.json``. Consumed by
the SCRAPE stage to enumerate docs and verify integrity before
normalize.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from lies.collections.errors import CollectionConfigInvalid

_MANIFEST_NAME = "manifest.json"


@dataclass(frozen=True)
class FileEntry:
    path: str
    sha256: str


class ScraperManifest:
    @staticmethod
    def _manifest_path(raw_dir: Path) -> Path:
        return raw_dir / _MANIFEST_NAME

    @staticmethod
    def read(raw_dir: Path) -> list[FileEntry]:
        p = ScraperManifest._manifest_path(raw_dir)
        if not p.exists():
            return []
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CollectionConfigInvalid(f"malformed manifest at {p}: {exc}") from exc
        return [FileEntry(path=e["path"], sha256=e["sha256"]) for e in payload["files"]]

    @staticmethod
    def write(raw_dir: Path, entries: list[FileEntry]) -> Path:
        raw_dir.mkdir(parents=True, exist_ok=True)
        p = ScraperManifest._manifest_path(raw_dir)
        payload = {"files": [{"path": e.path, "sha256": e.sha256} for e in entries]}
        p.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return p
