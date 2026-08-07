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

import contextlib
import json
import os
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
        # ``wiki`` intentionally untyped: ``lies.wiki.wiki`` is the
        # downstream consumer and importing it here creates a cycle.
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
        # bool is an int subclass; reject it explicitly.
        if not isinstance(version, int) or isinstance(version, bool):
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

    @staticmethod
    def save(wiki, registry: Registry) -> None:
        """Atomically write ``registry`` to ``wiki.registry_path``.

        Writes to ``<path>.tmp`` first, then ``os.replace`` for
        POSIX-atomic swap on the same filesystem. Cleans up the temp
        file on any failure before re-raising.

        Parent-directory fsync is intentionally omitted: the next
        register rebuilds the truth, so a power-loss between
        ``os.replace`` and the directory-entry fsync costs at most one
        registration.
        """
        from lies.collections.errors import RegistryWriteFailed

        path = wiki.registry_path
        tmp = path.with_suffix(path.suffix + ".tmp")
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": registry.version,
            "collections": {
                cid: ref.model_dump(mode="json") for cid, ref in registry.collections.items()
            },
        }
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(payload, fh, sort_keys=True)
                fh.flush()
                if hasattr(os, "fsync"):
                    os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException as exc:
            with contextlib.suppress(OSError):
                tmp.unlink()
            if isinstance(exc, OSError):
                raise RegistryWriteFailed(path, str(exc)) from exc
            raise

    @staticmethod
    def merge(a: Registry, b: Registry) -> Registry:
        """Return the union of two registries; ``b`` wins on ``collection_id`` collision."""
        merged: dict[str, WikiCollectionRef] = dict(a.collections)
        merged.update(b.collections)
        return Registry(collections=merged)

    @staticmethod
    def filter_stale(registry: Registry, wiki) -> Registry:
        """Drop entries whose ``<id>.yaml`` is missing under ``wiki.collections_dir``.

        ``wiki`` intentionally untyped: see ``Registry.load``.
        """
        kept = {
            cid: ref
            for cid, ref in registry.collections.items()
            if (wiki.collections_dir / f"{cid}.yaml").exists()
        }
        return Registry(collections=kept)
