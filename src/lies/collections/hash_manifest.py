"""Hash manifest — per-collection doc-path → sha256 map.

Stored at ``<wiki>/.lies/hashes/<collection>.json``. Compare on
ingest decides whether a doc is skipped (hash unchanged) or rewritten.
"""
from __future__ import annotations

import json
from pathlib import Path

_HASH_DIR = "hashes"
_MANIFEST_FILENAME = "{collection}.json"


class HashManifest:
    def __init__(self, wiki_root: Path, collection: str) -> None:
        self._wiki_root = wiki_root
        self._collection = collection
        self._path = self._manifest_path()
        self._data: dict[str, str] = self._load()

    def _manifest_path(self) -> Path:
        return self._wiki_root / ".lies" / _HASH_DIR / _MANIFEST_FILENAME.format(
            collection=self._collection
        )

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
