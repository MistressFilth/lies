"""Wiki management sub-app: collections list / show / new / modify / delete.

The ``collections_app = typer.Typer(...)`` instance lives here so
``__init__.py`` can ``app.add_typer(...)`` it.

Heavy imports stay inside each command body: yaml, the agent
factory, scrapers, the WikiMemoryService, the datetime/prompt helpers.
Only the sub-app object construction runs at ``import lies.cli`` time,
and that is cheap.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import warnings
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, cast

import typer
import yaml  # type: ignore[import-untyped]

from lies.wiki_settings import resolve_language

__all__ = (
    "collections_app",
    "collections_delete",
    "collections_enrich_tags",
    "collections_list",
    "collections_modify",
    "collections_new",
    "collections_show",
)


# ---------------------------------------------------------------------------
# Private wiki-yaml Collection helpers.
#
# These are the surviving implementation of the legacy wiki-yaml
# collection module. The library cutover moved canonical collection
# configs to ``<library>/collections/<slug>/config.yaml``
# (LibraryCollectionConfig + lies.library.config_io), but the wiki CLI
# sub-app here still operates on per-wiki YAML configs at
# ``wiki.collections_dir/<name>.yaml`` to preserve operator muscle memory
# for the existing CLI surface. The classes are private (``_`` prefix) and
# module-local: callers outside this file should use ``LibraryCollectionConfig``
# / ``lies.library.config_io`` instead.
# ---------------------------------------------------------------------------

_OPERATOR_CHARS_RE = re.compile(r"[+&|\-]")


class _CollectionError(Exception):
    """Base class for wiki-yaml collection-level errors."""


class _CollectionNotFound(_CollectionError):
    """Requested collection does not exist in the wiki."""


class _CollectionConfigInvalid(_CollectionError):
    """Collection config is malformed or fails validation."""


@dataclass(frozen=True)
class _Collection:
    """Per-wiki YAML collection config record.

    Mirrors the legacy wiki-yaml Collection shape so the
    wiki CLI commands keep the same observable behavior. The ``path``
    field was dropped from the library config because the per-wiki raw
    directory it pointed at never existed on disk; the wiki CLI keeps it
    as a stored field for backward compatibility with existing YAMLs.
    """

    name: str
    path: Path
    source: str
    tags: list[str]
    scraper_cmd: str | None
    doc_path: Path | None
    mapper_model: str | None
    language: str | None
    version: str
    created_at: datetime
    updated_at: datetime
    config: dict[str, Any] = field(default_factory=dict)

    def rejects_operator_chars(self) -> None:
        """Reject names containing reserved QMD operator characters."""
        if _OPERATOR_CHARS_RE.search(self.name):
            raise _CollectionConfigInvalid(
                f"collection name contains reserved characters: {self.name!r}"
            )

    @staticmethod
    def config_path(wiki: Any, name: str) -> Path:
        """Return the on-disk YAML path for a collection under ``wiki``."""
        return wiki.collections_dir / f"{name}.yaml"


def _parse_dt(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    raise _CollectionConfigInvalid(f"invalid datetime: {value!r}")


def _load_collection(wiki: Any, name: str) -> _Collection:
    config_path = _Collection.config_path(wiki, name)
    if not config_path.exists():
        raise _CollectionNotFound(f"collection {name!r} not found at {config_path}")
    try:
        payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise _CollectionConfigInvalid(f"invalid YAML in {config_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise _CollectionConfigInvalid(f"config root must be a mapping: {config_path}")
    try:
        raw_lang = payload.get("language")
        if raw_lang is None:
            language: str | None = None
        elif isinstance(raw_lang, str):
            stripped = raw_lang.strip()
            language = stripped if stripped else None
        else:
            raise _CollectionConfigInvalid(
                f"language must be a string, got {type(raw_lang).__name__}"
            )
        raw_tags = payload.get("tags", [])
        if isinstance(raw_tags, list):
            tags = [str(t) for t in raw_tags]
        elif raw_tags is None:
            tags = []
        else:
            warnings.warn(
                f"tags must be a list, got {type(raw_tags).__name__}; coercing to empty list",
                UserWarning,
                stacklevel=2,
            )
            tags = []
        collection = _Collection(
            name=payload["name"],
            path=Path(payload["path"]),
            source=payload["source"],
            tags=tags,
            scraper_cmd=payload.get("scraper_cmd"),
            doc_path=Path(payload["doc_path"]) if payload.get("doc_path") else None,
            mapper_model=payload.get("mapper_model"),
            language=language,
            version=payload["version"],
            created_at=_parse_dt(payload["created_at"]),
            updated_at=_parse_dt(payload["updated_at"]),
            config=payload.get("config") or {},
        )
    except KeyError as exc:
        raise _CollectionConfigInvalid(f"missing field {exc} in {config_path}") from exc
    collection.rejects_operator_chars()
    return collection


def _save_collection(wiki: Any, collection: _Collection) -> None:
    collection.rejects_operator_chars()
    payload = asdict(collection)
    payload["path"] = str(collection.path)
    payload["doc_path"] = str(collection.doc_path) if collection.doc_path else None
    payload["created_at"] = collection.created_at.isoformat()
    payload["updated_at"] = collection.updated_at.isoformat()
    config_path = _Collection.config_path(wiki, collection.name)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = config_path.with_suffix(config_path.suffix + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            yaml.safe_dump(payload, fh, sort_keys=True)
            fh.flush()
            if hasattr(os, "fsync"):
                os.fsync(fh.fileno())
        os.replace(tmp, config_path)
    except BaseException as exc:
        with contextlib.suppress(OSError):
            tmp.unlink()
        if isinstance(exc, OSError):
            raise _CollectionConfigInvalid(f"failed to write {config_path}: {exc}") from exc
        raise


# ---------------------------------------------------------------------------


collections_app = typer.Typer(
    help="Inspect, modify, and author collection configurations.",
)


@collections_app.command("list")
def collections_list(
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Emit a JSON array of {name, source, tags, language, sync_status} per collection.",
        ),
    ] = False,
    name: Annotated[
        str | None,
        typer.Option(
            "--name",
            envvar="LIES_WIKI_NAME",
            help="Wiki to inspect (defaults to $LIES_WIKI_NAME).",
        ),
    ] = None,
) -> None:
    """List every collection in the wiki's collections dir with source, tags, and sync status."""
    from lies.cli import resolve_wiki
    from lies.memory.service import WikiMemoryService

    wiki = resolve_wiki(name)
    cfg_dir = wiki.collections_dir
    stems = sorted(p.stem for p in cfg_dir.glob("*.yaml"))
    if json_output:
        svc = WikiMemoryService(wiki)
        registered = svc.registered_collections()
        registered_ids = {r.collection_id for r in registered}
        rows: list[dict[str, object]] = []
        for stem in stems:
            c = _load_collection(wiki, stem)
            rows.append(
                {
                    "name": c.name,
                    "source": c.source,
                    "tags": c.tags,
                    "language": resolve_language(wiki, c),  # ty: ignore[invalid-argument-type]
                    "sync_status": "registered" if stem in registered_ids else "pending",
                }
            )
        typer.echo(json.dumps(rows, indent=2))
        return
    # Default text output: one stem per line (preserves the pre-split
    # ``lies collections list`` behavior verbatim).
    for stem in stems:
        typer.echo(stem)


@collections_app.command("show")
def collections_show(
    collection_name: Annotated[str, typer.Argument(help="Collection name (e.g. 'claude-code').")],
    name: Annotated[
        str | None,
        typer.Option(
            "--name",
            envvar="LIES_WIKI_NAME",
            help="Wiki containing the collection.",
        ),
    ] = None,
) -> None:
    """Show a single collection's full configuration: source, tags, language, registered status."""
    from lies.cli import resolve_wiki
    from lies.memory.service import WikiMemoryService

    wiki = resolve_wiki(name)
    c = _load_collection(wiki, collection_name)
    typer.echo(f"name={c.name} source={c.source} tags={c.tags}")
    typer.echo(f"language: {resolve_language(wiki, c)}")  # ty: ignore[invalid-argument-type]
    # The CLI doesn't know whether sync has run in this process;
    # an empty registry means the in-process WikiMemoryService for
    # this wiki root has not registered any collection yet.
    svc = WikiMemoryService(wiki)
    registered = svc.registered_collections()
    ref = next(
        (r for r in registered if r.collection_id == collection_name),
        None,
    )
    typer.echo(f"status: {'registered' if ref else 'pending'}")


@collections_app.command("new")
def collections_new(
    collection_name: Annotated[str, typer.Argument(help="Collection name to create.")],
    *,
    source: Annotated[
        str | None,
        typer.Option(
            help="Source path or URL for the collection (e.g. a local dir or git URL).",
        ),
    ] = None,
    prompt: Annotated[
        str | None,
        typer.Option(help="Path to a prompt file; reads stdin if omitted."),
    ] = None,
    apply: Annotated[
        bool,
        typer.Option(
            "--apply/--no-apply",
            help="Write the collection YAML to disk; without --apply the config is printed to stdout only.",
        ),
    ] = False,
    tag: Annotated[
        list[str] | None,
        typer.Option(
            "--tag",
            help="Tag to attach to the new collection (repeatable); merged with the agent's proposed tags.",
        ),
    ] = None,
    name: Annotated[
        str | None,
        typer.Option(
            "--name",
            envvar="LIES_WIKI_NAME",
            help="Wiki to create the collection in.",
        ),
    ] = None,
) -> None:
    """Create a new collection via the interactive wizard."""
    from rich.prompt import Prompt

    # Import the agent inside the body so that tests can mock
    # ``lies.agents.collection_author.collection_author_agent`` at
    # the source module. Module-level imports would freeze the
    # reference before the mock applies.
    from lies.agents.collection_author import (
        AuthorProposal as _AuthorProposal,
    )
    from lies.agents.collection_author import (
        AuthorQuestion as _AuthorQuestion,
    )
    from lies.agents.collection_author import (
        CollectionAuthorDeps as _AuthorDeps,
    )
    from lies.agents.collection_author import (
        collection_author_agent as _factory,
    )
    from lies.cli import pick_scraper, resolve_wiki

    wiki = resolve_wiki(name)
    cfg_dir = wiki.collections_dir
    if not source or not prompt:
        raise typer.BadParameter("collections new requires --source and --prompt")
    # Manifest-only fetch (no body). The scraper's emit_manifest
    # expects a list of ParsedDoc; an empty list produces an empty
    # manifest, which is fine -- the agent uses it to ask format
    # questions and the user supplies the rest.
    scraper = pick_scraper(source)
    scratch_dir = wiki.scratch_dir
    manifest_path = scraper.emit_manifest([], scratch_dir)
    manifest: list[dict[str, object]] = []
    if manifest_path and manifest_path.exists():
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest = list(data.get("files", []))
    agent = _factory()
    history: list[object] = []
    deps = _AuthorDeps(manifest=manifest)
    while True:
        # ``message_history`` expects a typed Sequence of model
        # messages; we accept arbitrary user-prompt injections from the
        # rich-prompt loop, so cast to Any at the boundary.
        result = agent.run_sync(
            prompt,
            deps=deps,
            message_history=cast(Any, history),
        )
        history.append(result.new_messages())
        out = result.output
        if isinstance(out, _AuthorQuestion):
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
        if isinstance(out, _AuthorProposal):
            typer.echo(yaml.safe_dump(out.collection, sort_keys=True))
            if apply:
                now = datetime.now(tz=UTC)
                payload = dict(out.collection)
                payload.setdefault("name", collection_name)
                payload.setdefault("path", str(wiki.data_root / "raw" / collection_name))
                payload.setdefault("created_at", now)
                payload.setdefault("updated_at", now)
                # Merge --tag values into the agent's proposed tags list
                # (de-duplicated, preserving order, agent's tags first).
                # Matches collections_modify's --tag/--untag merge semantics
                # so operators get the same behavior across both verbs.
                if tag:
                    existing_tags = [str(t) for t in payload.get("tags") or []]
                    merged = list(existing_tags)
                    for t in tag:
                        if t and t not in merged:
                            merged.append(t)
                    payload["tags"] = merged
                # The agent may emit ISO strings; coerce to datetime
                # so _Collection's typed fields and _save_collection's
                # .isoformat() call work either way.
                created = payload.get("created_at")
                updated = payload.get("updated_at")
                if isinstance(created, str):
                    payload["created_at"] = datetime.fromisoformat(created)
                if isinstance(updated, str):
                    payload["updated_at"] = datetime.fromisoformat(updated)
                payload["path"] = Path(payload["path"])
                doc_path = payload.get("doc_path")
                if doc_path is not None:
                    payload["doc_path"] = Path(doc_path)
                collection = _Collection(**payload)
                _save_collection(wiki, collection)
                typer.echo(f"wrote {cfg_dir / (collection_name + '.yaml')}")
            return
        raise typer.BadParameter("agent returned unexpected output")


@collections_app.command("modify")
def collections_modify(
    collection_name: Annotated[str, typer.Argument(help="Collection name to modify.")],
    *,
    set_: Annotated[
        list[str] | None,
        typer.Option(
            "--set",
            help="KEY=VALUE pair to set (repeatable, e.g. --set source=./new --set doc_path=./new.md).",
        ),
    ] = None,
    tag: Annotated[
        list[str] | None,
        typer.Option("--tag", help="Tag to add (repeatable)."),
    ] = None,
    untag: Annotated[
        list[str] | None,
        typer.Option("--untag", help="Tag to remove (repeatable)."),
    ] = None,
    from_file: Annotated[
        Path | None,
        typer.Option(
            "--from-file",
            help="Path to a YAML patch file with editable fields (mutually exclusive with --set).",
        ),
    ] = None,
    name: Annotated[
        str | None,
        typer.Option(
            "--name",
            envvar="LIES_WIKI_NAME",
            help="Wiki containing the collection.",
        ),
    ] = None,
) -> None:
    """Mutate an existing collection's source, tags, or other fields."""
    from lies.cli import resolve_wiki

    wiki = resolve_wiki(name)
    if from_file is not None and set_:
        raise typer.BadParameter("modify accepts --from-file or --set, not both")
    if from_file is None and not set_ and not tag and not untag:
        raise typer.BadParameter("modify requires --from-file, --set, --tag, or --untag")

    existing = _load_collection(wiki, collection_name)

    editable_top = {
        "source",
        "tags",
        "scraper_cmd",
        "doc_path",
        "mapper_model",
        "language",
        "config",
    }

    updates: dict[str, object] = {}

    if from_file is not None:
        if not from_file.exists():
            raise typer.BadParameter(f"file not found: {from_file}")
        try:
            payload = yaml.safe_load(from_file.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise typer.BadParameter(f"invalid YAML in {from_file}: {exc}") from exc
        if not isinstance(payload, dict):
            raise typer.BadParameter("--from-file root must be a mapping")
        reserved = {"name", "path", "version", "created_at", "updated_at"}
        for k in payload:
            if k in reserved:
                raise typer.BadParameter(f"--from-file cannot set {k!r}; reserved")
            if k not in editable_top:
                raise typer.BadParameter(
                    f"--from-file key {k!r} not editable; allowed: {sorted(editable_top)}"
                )
        if "doc_path" in payload and payload["doc_path"] is not None:
            payload["doc_path"] = Path(payload["doc_path"])
        if "tags" in payload:
            payload["tags"] = list(payload["tags"])
        if "config" in payload and payload["config"] is None:
            payload["config"] = {}
        updates.update(payload)

    if set_:
        for raw in set_:
            if "=" not in raw:
                raise typer.BadParameter(f"--set expects KEY=VALUE, got {raw!r}")
            key, value = raw.split("=", 1)
            key = key.strip()
            value = value.strip()
            if key.startswith("config."):
                sub = key.split(".", 1)[1]
                if not sub:
                    raise typer.BadParameter(f"--set {key!r}: empty subkey")
                cfg_src = updates.get("config")
                cfg = dict(cast("dict[str, Any]", cfg_src)) if cfg_src else dict(existing.config)
                cfg[sub] = value
                updates["config"] = cfg
                continue
            if key not in editable_top:
                raise typer.BadParameter(
                    f"key {key!r} not editable; allowed: {sorted(editable_top)}"
                )
            if key == "tags":
                parts = [p.strip() for p in value.split(",")]
                parts = [p for p in parts if p]
                if not parts:
                    raise typer.BadParameter("tags value cannot be empty")
                updates["tags"] = parts
            elif key == "doc_path":
                if not value:
                    raise typer.BadParameter("doc_path cannot be empty")
                updates["doc_path"] = Path(value)
            else:
                updates[key] = value

    # --tag / --untag merge into the existing tags list (or the
    # --from-file / --set tags update). Additive on top of --set so
    # callers can ``--set tags=a,b --tag c --untag b`` in one shot.
    if tag or untag:
        cur_tags_src = updates.get("tags", existing.tags)
        cur_tags = list(cast(list[str], cur_tags_src))
        if tag:
            for t in tag:
                if t and t not in cur_tags:
                    cur_tags.append(t)
        if untag:
            cur_tags = [t for t in cur_tags if t not in set(untag)]
        updates["tags"] = cur_tags

    updates["updated_at"] = datetime.now(tz=UTC)
    new = replace(existing, **updates)
    _save_collection(wiki, new)
    typer.echo(f"updated {_Collection.config_path(wiki, collection_name)}")


@collections_app.command("delete")
def collections_delete(
    collection_name: Annotated[str, typer.Argument(help="Collection name to delete.")],
    *,
    force: Annotated[bool, typer.Option("--force", help="Skip the confirmation prompt.")] = False,
    name: Annotated[
        str | None,
        typer.Option(
            "--name",
            envvar="LIES_WIKI_NAME",
            help="Wiki containing the collection.",
        ),
    ] = None,
) -> None:
    """Delete a collection's YAML config (does not touch the source path)."""
    from rich.prompt import Confirm

    from lies.cli import resolve_wiki

    wiki = resolve_wiki(name)
    cfg_path = wiki.collections_dir / f"{name}.yaml"
    if not cfg_path.exists():
        raise typer.BadParameter(f"collection {name!r} not found at {cfg_path}")
    if not force and not Confirm.ask(f"Delete {cfg_path}?", default=False):
        typer.echo("aborted")
        raise typer.Exit(code=0)
    cfg_path.unlink()
    typer.echo(f"deleted {cfg_path}")


@collections_app.command("enrich-tags")
def collections_enrich_tags(
    name: Annotated[
        str | None,
        typer.Option(
            "--name",
            envvar="LIES_WIKI_NAME",
            help="Wiki to enrich (default: $LIES_WIKI_NAME).",
        ),
    ] = None,
    apply: Annotated[
        bool,
        typer.Option(
            "--apply/--no-apply",
            help="Apply the proposed --set tags=... invocations; default is dry-run (prints to stdout).",
        ),
    ] = False,
) -> None:
    """Print ``lies collections modify <name> --set tags=...`` for collections with empty tags.

    Walks the wiki's ``collections_dir`` (mirroring ``lies collections
    list``) and emits one hint per YAML whose ``tags`` field is empty
    or missing. Dry-run by default: the operator reviews the printed
    list, then re-runs ``lies collections modify <name> --set tags=X,Y``
    for each line. ``--apply`` is reserved for a future auto-apply
    implementation and currently raises.
    """
    from lies.cli import resolve_wiki

    wiki = resolve_wiki(name)
    cfg_dir = wiki.collections_dir
    for cfg_path in sorted(cfg_dir.glob("*.yaml")):
        try:
            coll = _load_collection(wiki, cfg_path.stem)
        except (_CollectionNotFound, _CollectionConfigInvalid):
            # Skip malformed configs so one bad file does not mask the
            # rest of the dry-run output.
            continue
        if coll.tags:
            continue
        typer.echo(f"lies collections modify {coll.name} --set tags=<comma-separated>")
    if apply:
        raise typer.BadParameter(
            "enrich-tags does not auto-apply; run the printed commands, "
            "or use --set tags=... directly."
        )
