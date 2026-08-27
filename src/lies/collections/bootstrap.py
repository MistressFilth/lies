"""Idempotent wiki + collection bootstrap for ingest/sync/ingest_source.

Three CLI commands (``ingest``, ``sync``, ``ingest_source``) call
``ensure_wiki`` then ``bootstrap_collection`` before their normal sync or
ingest path. Both helpers are safe to call when the target already exists.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from lies.collections.errors import CollectionMismatch, WikiLayoutInitFailed
from lies.collections.record import Collection, load_collection, save_collection
from lies.wiki.layout import WikiLayout  # noqa: F401 - patch target for tests
from lies.wiki.wiki import Wiki


def ensure_wiki(name: str) -> Wiki:
    """Resolve a wiki by name, auto-initializing it if it does not exist.

    Raises ``WikiLayoutInitFailed`` if the auto-init step fails.
    """
    from lies.cli import resolve_wiki  # late import: cli layer
    from lies.cli._core import _init_wiki_internal
    from lies.errors import WikiNotRegistered

    try:
        return resolve_wiki(name)
    except WikiNotRegistered:
        pass

    wiki = Wiki(
        name=name,
        data_root=Wiki.data_root_for(name),
        config_root=_config_root_for(name),
        cache_root=_cache_root_for(name),
        state_root=_state_root_for(name),
        runtime_root=_runtime_root_for(name),
    )
    try:
        _init_wiki_internal(wiki)
    except Exception as exc:
        raise WikiLayoutInitFailed(name, exc) from exc
    return resolve_wiki(name)


def bootstrap_collection(
    wiki: Wiki,
    name: str,
    source: str,
    *,
    wizard: bool = False,
) -> Collection:
    """Idempotently ensure a collection YAML exists for ``(wiki, name)``.

    - YAML exists + ``source`` matches: return the existing Collection.
    - YAML exists + ``source`` differs: raise ``CollectionMismatch``.
    - YAML missing + ``wizard=False``: write a minimal Collection and return it.
    - YAML missing + ``wizard=True``: raise ``NotImplementedError`` until
      Task 4 wires the wizard.

    ``source`` comparison is on the raw string. An empty existing ``source``
    is treated as "unknown" and never raises.
    """
    if wizard:
        raise NotImplementedError("wizard mode lands in Task 4")
    config_path = Collection.config_path(wiki, name)
    if config_path.exists():
        existing = load_collection(wiki, name)
        existing_source = (existing.source or "").strip()
        if existing_source and existing_source != source:
            raise CollectionMismatch(
                existing_source=existing.source,
                existing_format=None,
                requested_source=source,
                requested_format=None,
            )
        return existing

    now = datetime.now(tz=UTC)
    new = Collection(
        name=name,
        path=wiki.data_root / "raw" / name,
        source=source,
        tags=[],
        scraper_cmd=None,
        doc_path=None,
        mapper_model=None,
        language=None,
        version="1",
        created_at=now,
        updated_at=now,
        config={},
    )
    save_collection(wiki, new)
    return new


def _role_root_for(name: str, home_fn: Callable[[], Path]) -> Path:
    """Return ``<home_fn()>/LIES_DATA_SUBDIR/<name>`` for any XDG role."""
    from lies.constants import LIES_DATA_SUBDIR

    return home_fn() / LIES_DATA_SUBDIR / name


def _config_root_for(name: str) -> Path:
    from lies import xdg

    return _role_root_for(name, xdg.config_home)


def _cache_root_for(name: str) -> Path:
    from lies import xdg

    return _role_root_for(name, xdg.cache_home)


def _state_root_for(name: str) -> Path:
    from lies import xdg

    return _role_root_for(name, xdg.state_home)


def _runtime_root_for(name: str) -> Path:
    from lies import xdg

    return _role_root_for(name, xdg.runtime_dir)
