"""Hash manifest — per-collection doc-path → sha256 map.

Stored in the wiki's XDG cache directory. Compare on ingest decides
whether a doc is skipped (hash unchanged) or rewritten.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lies.wiki.wiki import Wiki

_MANIFEST_FILENAME = "{collection}.json"


class HashManifest:
    def __init__(self, wiki: Wiki, collection: str) -> None:
        from lies.wiki.wiki import Wiki

        if not isinstance(wiki, Wiki):
            raise TypeError("HashManifest requires a Wiki instance")
        self._wiki = wiki
        self._collection = collection
        self._path = wiki.hashes_dir / _MANIFEST_FILENAME.format(collection=collection)
        self._data: dict[str, str] = self._load()

    @property
    def path(self) -> Path:
        """The on-disk path the manifest reads from and writes to."""
        return self._path

    def _load(self) -> dict[str, str]:
        if not self._path.exists():
            return {}
        try:
            loaded = json.loads(self._path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        if not isinstance(loaded, dict):
            return {}
        return {str(k): str(v) for k, v in loaded.items()}

    def read(self) -> dict[str, str]:
        return dict(self._data)

    def compare(self, path: str, sha256: str) -> bool:
        return self._data.get(path) == sha256

    def update(self, path: str, sha256: str) -> None:
        self._data[path] = sha256

    def flush(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._data, indent=2), encoding="utf-8")

    def snapshot(self) -> Path:
        snap_path = self._wiki.hashes_dir / f"{self._collection}.pre-sync.json"
        snap_path.parent.mkdir(parents=True, exist_ok=True)
        snap_path.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        return snap_path

    def restore(self, snapshot: Path) -> None:
        self._data = json.loads(snapshot.read_text(encoding="utf-8"))
        self.flush()
