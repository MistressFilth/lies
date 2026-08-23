"""Wiki management sub-app: collections list / show / new / modify / delete.

The ``collections_app = typer.Typer(...)`` instance lives here so
``__init__.py`` can ``app.add_typer(...)`` it.

Heavy imports stay inside each command body: yaml, the agent
factory, scrapers, the WikiMemoryService, the datetime/prompt helpers.
Only the sub-app object construction runs at ``import lies.cli`` time,
and that is cheap.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, cast

import typer

from lies.wiki_settings import resolve_language

__all__ = (
    "collections_app",
    "collections_delete",
    "collections_list",
    "collections_modify",
    "collections_new",
    "collections_show",
)


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
    from lies.collections.record import load_collection
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
            c = load_collection(wiki, stem)
            rows.append(
                {
                    "name": c.name,
                    "source": c.source,
                    "tags": c.tags,
                    "language": resolve_language(wiki, c),
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
    from lies.collections.record import load_collection
    from lies.memory.service import WikiMemoryService

    wiki = resolve_wiki(name)
    c = load_collection(wiki, collection_name)
    typer.echo(f"name={c.name} source={c.source} tags={c.tags}")
    typer.echo(f"language: {resolve_language(wiki, c)}")
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
    import yaml  # type: ignore[import-untyped]
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
    from lies.collections.record import Collection, save_collection

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
        # messages; we accept arbitrary user-prompt injections from
        # the rich-prompt loop, so cast to Any at the boundary.
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
                # The agent may emit ISO strings; coerce to datetime
                # so Collection's typed fields and save_collection's
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
                collection = Collection(**payload)
                save_collection(wiki, collection)
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
    from dataclasses import replace as _dc_replace

    import yaml  # type: ignore[import-untyped]

    from lies.cli import resolve_wiki
    from lies.collections.record import Collection, load_collection, save_collection

    wiki = resolve_wiki(name)
    if from_file is not None and set_:
        raise typer.BadParameter("modify accepts --from-file or --set, not both")
    if from_file is None and not set_ and not tag and not untag:
        raise typer.BadParameter("modify requires --from-file, --set, --tag, or --untag")

    existing = load_collection(wiki, collection_name)

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
    new = _dc_replace(existing, **updates)
    save_collection(wiki, new)
    typer.echo(f"updated {Collection.config_path(wiki, collection_name)}")


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
