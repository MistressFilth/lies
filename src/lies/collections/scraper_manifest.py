"""Scraper manifest — file list + content hashes from the scraper.

Stored under the wiki's XDG cache root. Consumed by the SCRAPE stage to
enumerate docs and verify integrity before normalize.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from lies.collections.errors import CollectionConfigInvalid

if TYPE_CHECKING:
    from lies.wiki.wiki import Wiki

_MANIFEST_NAME = "manifest.json"


@dataclass(frozen=True)
class FileEntry:
    path: str
    sha256: str


def _manifest_dir(wiki: Wiki, name: str) -> Path:
    return wiki.cache_root / "collections" / name


def _manifest_path(wiki: Wiki, name: str) -> Path:
    return _manifest_dir(wiki, name) / _MANIFEST_NAME


class ScraperManifest:
    @staticmethod
    def manifest_dir(wiki: Wiki, name: str) -> Path:
        """Return the on-disk directory holding the manifest for ``name``."""
        return _manifest_dir(wiki, name)

    @staticmethod
    def read(wiki: Wiki, name: str) -> list[FileEntry]:
        p = _manifest_path(wiki, name)
        if not p.exists():
            return []
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CollectionConfigInvalid(f"malformed manifest at {p}: {exc}") from exc
        return [FileEntry(path=e["path"], sha256=e["sha256"]) for e in payload["files"]]

    @staticmethod
    def write(wiki: Wiki, name: str, entries: list[FileEntry]) -> Path:
        manifest_dir = _manifest_dir(wiki, name)
        manifest_dir.mkdir(parents=True, exist_ok=True)
        p = manifest_dir / _MANIFEST_NAME
        payload = {"files": [{"path": e.path, "sha256": e.sha256} for e in entries]}
        p.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return p
