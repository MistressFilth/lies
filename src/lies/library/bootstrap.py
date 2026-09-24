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

:func:`bootstrap_all_missing_configs` is the sweep variant: iterates
every directory under ``Library.collections_root`` and bootstraps any
collection whose ``config.yaml`` is missing. Collections with an
existing config are left untouched (their real ``source`` wins over
the sweep placeholder). The sweep never invokes the wizard — a batch
operation should not depend on a TTY.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

from lies.library.config_io import config_path_for, load_config, save_config
from lies.library.errors import (
    CollectionMismatch,
    WizardRequiresTTY,
)
from lies.library.record import LibraryCollectionConfig


@dataclass(frozen=True)
class BootstrapAllReport:
    """Result summary for :func:`bootstrap_all_missing_configs`.

    ``created`` are slugs that gained a new ``config.yaml`` on this run;
    ``skipped`` are slugs that already had one and were left alone.
    ``skipped_sources`` mirrors ``skipped`` with the existing source
    surfaced, so the operator can audit which records were deemed
    trustworthy enough to leave in place.
    """

    created: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    skipped_sources: dict[str, str] = field(default_factory=dict)


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


def bootstrap_all_missing_configs(
    *,
    default_source_template: str = "bootstrap-all://{slug}",
) -> BootstrapAllReport:
    """Bootstrap a config for every library collection that lacks one.

    Iterates :func:`lies.library.registry.library_collection_names` and,
    for each directory without a ``config.yaml``, writes a minimal
    record whose ``source`` is ``default_source_template.format(slug=...)``.
    Collections that already have a config are left untouched: their
    existing ``source`` is the trusted value, and overwriting it with
    the sweep placeholder would be destructive.

    The placeholder source is intentionally not a URL: an operator who
    ran the sweep to repair a no-config directory should follow up with
    ``lies library modify <slug> --set source=<real-url>`` to record
    the actual upstream. The CLI surfaces the per-slug outcome so the
    follow-up is mechanical, not exploratory.

    Returns a :class:`BootstrapAllReport` describing what changed. The
    function never invokes the wizard: a batch sweep must not depend
    on a TTY, and the wizard does not know the real source anyway.
    """
    from lies.library.registry import library_collection_names

    created: list[str] = []
    skipped: list[str] = []
    skipped_sources: dict[str, str] = {}

    for slug in sorted(library_collection_names()):
        if config_path_for(slug).exists():
            existing = load_config(slug)
            skipped.append(slug)
            skipped_sources[slug] = existing.source
            continue
        _bootstrap_bare(slug, default_source_template.format(slug=slug))
        created.append(slug)

    return BootstrapAllReport(
        created=tuple(created),
        skipped=tuple(skipped),
        skipped_sources=skipped_sources,
    )


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
