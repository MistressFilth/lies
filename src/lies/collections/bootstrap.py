"""Idempotent wiki + collection bootstrap for ingest/sync/ingest_source.

Three CLI commands (``ingest``, ``sync``, ``ingest_source``) call
``ensure_wiki`` then ``bootstrap_collection`` before their normal sync or
ingest path. Both helpers are safe to call when the target already exists.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from lies.agents.collection_author import (
    AuthorProposal,
    AuthorQuestion,
    CollectionAuthorDeps,
    collection_author_agent,
)
from lies.collections.errors import (
    CollectionMismatch,
    WikiLayoutInitFailed,
    WizardAborted,
    WizardRequiresTTY,
)
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
    - YAML missing + ``wizard=True``: drive ``collection_author_agent``
      interactively and persist the resulting ``AuthorProposal``.

    ``source`` comparison is on the raw string. An empty existing ``source``
    is treated as "unknown" and never raises.
    """
    if wizard and not sys.stdin.isatty():
        raise WizardRequiresTTY()

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

    if wizard:
        return _bootstrap_via_wizard(wiki, name, source)
    return _bootstrap_bare(wiki, name, source)


def _bootstrap_bare(wiki: Wiki, name: str, source: str) -> Collection:
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


def _bootstrap_via_wizard(wiki: Wiki, name: str, source: str) -> Collection:
    """Drive ``collection_author_agent`` interactively, then save the proposal.

    Mirrors the Q&A loop in ``src/lies/cli/collections.py:188-242`` so the
    CLI ``lies collections new`` flow and the bootstrap path use the same
    agent contract. Loop exits when the agent returns an ``AuthorProposal``;
    any other output type aborts the wizard with ``WizardAborted``.
    """
    from rich.prompt import Prompt

    prompt = (
        f"{source} — describe how to ingest this corpus. Use tags to mark "
        f"sections; set scraper_cmd only if the default scraper is wrong."
    )
    # Reference the agent factory via its module-level name so tests can
    # ``mock.patch("lies.collections.bootstrap.collection_author_agent", ...)``.
    agent = collection_author_agent()
    history: list[object] = []
    deps = CollectionAuthorDeps(manifest=[])
    while True:
        # ``message_history`` expects a typed Sequence of model messages;
        # we accept arbitrary user-prompt injections from the rich-prompt
        # loop, so cast to Any at the boundary.
        result = agent.run_sync(
            prompt,
            deps=deps,
            message_history=cast(Any, history),
        )
        history.append(result.new_messages())
        out = result.output
        if isinstance(out, AuthorQuestion):
            if out.options:
                answer = Prompt.ask(
                    out.prompt,
                    choices=out.options,
                    default=out.default or out.options[0],
                )
            elif out.default is not None:
                answer = Prompt.ask(out.prompt, default=out.default)
            else:
                answer = Prompt.ask(out.prompt)
            history.append({"role": "user", "content": f"{out.id}: {answer}"})
            continue
        if isinstance(out, AuthorProposal):
            now = datetime.now(tz=UTC)
            payload = dict(out.collection)
            payload.setdefault("name", name)
            payload.setdefault("path", str(wiki.data_root / "raw" / name))
            payload.setdefault("created_at", now)
            payload.setdefault("updated_at", now)
            # The agent may emit ISO strings; coerce to datetime so
            # Collection's typed fields and save_collection's .isoformat()
            # work either way.
            for key in ("created_at", "updated_at"):
                if isinstance(payload.get(key), str):
                    payload[key] = datetime.fromisoformat(cast(str, payload[key]))
            payload["path"] = Path(payload["path"])
            doc_path = payload.get("doc_path")
            if doc_path is not None:
                payload["doc_path"] = Path(doc_path)
            coll = Collection(**payload)
            save_collection(wiki, coll)
            return coll
        raise WizardAborted()


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
