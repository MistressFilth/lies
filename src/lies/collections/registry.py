"""Per-wiki collection registry persistence.

Stores the set of :class:`WikiCollectionRef` values that
``WikiMemoryService`` has successfully registered. Persists to the
wiki's XDG state root so the registry survives process boundaries.

File format (version 1)::

    {
      "version": 1,
      "collections": {
        "<collection_id>": WikiCollectionRef dict,
        ...
      }
    }

Writes are atomic via ``os.replace(<tmp>, <final>)``. Loads tolerate
missing files (return empty registry) but propagate corruption and
unsupported-version errors so silent loss of registry state is not
possible.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from lies.collections.errors import (
    RegistryCorrupt,
    RegistryVersionUnsupported,
)
from lies.memory.models import WikiCollectionRef

_SUPPORTED_VERSION = 1


@dataclass(frozen=True)
class Registry:
    """In-memory view of a wiki's persisted collection registry."""

    collections: dict[str, WikiCollectionRef]
    version: Literal[1] = 1

    @staticmethod
    def load(wiki) -> Registry:
        """Load the registry from ``wiki.registry_path``.

        Missing or empty file returns a fresh registry. Corrupt JSON
        or unknown version raises a typed error.
        """
        path = wiki.registry_path
        if not path.exists():
            return Registry(collections={})
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RegistryCorrupt(path, f"read failed: {exc}") from exc
        if not text.strip():
            return Registry(collections={})
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RegistryCorrupt(path, f"invalid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise RegistryCorrupt(path, "root must be a JSON object")
        version = payload.get("version")
        if not isinstance(version, int):
            raise RegistryVersionUnsupported(path, found=-1, supported=_SUPPORTED_VERSION)
        if version > _SUPPORTED_VERSION:
            raise RegistryVersionUnsupported(path, found=version, supported=_SUPPORTED_VERSION)
        raw = payload.get("collections") or {}
        if not isinstance(raw, dict):
            raise RegistryCorrupt(path, "`collections` must be an object")
        collections: dict[str, WikiCollectionRef] = {}
        for cid, ref_payload in raw.items():
            if not isinstance(ref_payload, dict):
                raise RegistryCorrupt(path, f"entry {cid!r} must be an object")
            try:
                collections[cid] = WikiCollectionRef(**ref_payload)
            except (TypeError, ValueError) as exc:
                raise RegistryCorrupt(path, f"entry {cid!r} invalid: {exc}") from exc
        return Registry(collections=collections)
