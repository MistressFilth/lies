"""Idempotent library-collection config bootstrap.

Mirrors the contract of the legacy wiki-scoped bootstrap helper but
without a ``Wiki`` argument: collections are library-global, so
bootstrap is too.

Behavior:

- ``config.yaml`` exists + ``source`` matches → return existing record.
- ``config.yaml`` exists + ``source`` differs → raise :class:`CollectionMismatch`.
- ``config.yaml`` missing + ``wizard=False`` → write a minimal record and return it.
- ``config.yaml`` missing + ``wizard=True`` → raise :class:`WizardRequiresTTY`
  if stdin is not a TTY; otherwise drive the agent interactively.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from typing import Any, cast

from lies.library.config_io import config_path_for, load_config, save_config
from lies.library.errors import (
    CollectionMismatch,
    WizardRequiresTTY,
)
from lies.library.record import LibraryCollectionConfig


def bootstrap_library_collection(
    slug: str,
    source: str,
    *,
    wizard: bool = False,
) -> LibraryCollectionConfig:
    """Idempotently ensure a library-collection config exists for ``slug``."""
    path = config_path_for(slug)
    if path.exists():
        existing = load_config(slug)
        if existing.source.strip() and existing.source.strip() != source.strip():
            raise CollectionMismatch(
                existing_source=existing.source,
                requested_source=source,
            )
        return existing

    if wizard and not sys.stdin.isatty():
        raise WizardRequiresTTY()

    if wizard:
        return _bootstrap_via_wizard(slug, source)

    return _bootstrap_bare(slug, source)


def _bootstrap_bare(slug: str, source: str) -> LibraryCollectionConfig:
    now = datetime.now(tz=UTC)
    record = LibraryCollectionConfig(
        name=slug,
        source=source,
        tags=(),
        scraper_cmd=None,
        doc_path=None,
        mapper_model=None,
        language=None,
        version="1",
        created_at=now,
        updated_at=now,
        config={},
    )
    save_config(record)
    return record


def _bootstrap_via_wizard(slug: str, source: str) -> LibraryCollectionConfig:
    """Drive :func:`collection_author_agent` interactively."""
    from lies.agents.collection_author import (
        AuthorProposal,
        AuthorQuestion,
        CollectionAuthorDeps,
        collection_author_agent,
    )
    from rich.prompt import Prompt

    from lies.library.errors import WizardAborted

    prompt = (
        f"{source} — describe how to ingest this corpus. Use tags to mark "
        f"sections; set scraper_cmd only if the default scraper is wrong."
    )
    agent = collection_author_agent()
    history: list[object] = []
    deps = CollectionAuthorDeps(manifest=[])
    while True:
        result = agent.run_sync(prompt, deps=deps, message_history=cast(Any, history))
        history.append(result.new_messages())
        out = result.output
        if isinstance(out, AuthorQuestion):
            if out.options:
                answer = Prompt.ask(
                    out.prompt, choices=out.options, default=out.default or out.options[0]
                )
            elif out.default is not None:
                answer = Prompt.ask(out.prompt, default=out.default)
            else:
                answer = Prompt.ask(out.prompt)
            history.append({"role": "user", "content": f"{out.id}: {answer}"})
            continue
        if isinstance(out, AuthorProposal):
            from datetime import datetime as _dt

            now = _dt.now(tz=UTC)
            payload = dict(out.collection)
            payload.setdefault("name", slug)
            payload.setdefault("created_at", now.isoformat())
            payload.setdefault("updated_at", now.isoformat())
            payload["created_at"] = _dt.fromisoformat(payload["created_at"])
            payload["updated_at"] = _dt.fromisoformat(payload["updated_at"])
            doc_path = payload.get("doc_path")
            if doc_path is not None:
                from pathlib import Path as _Path

                payload["doc_path"] = _Path(doc_path)
            record = LibraryCollectionConfig(**payload)
            save_config(record)
            return record
        raise WizardAborted()
