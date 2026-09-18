"""``lies library`` sub-app: inspect and author library-collection configs.

The legacy ``lies collections`` group is gone. This sub-app exposes the
same verbs (list / show / new / modify / delete / enrich-tags) plus a
``where`` verb that prints the on-disk config path for a slug.
"""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer

from lies.library.config_io import config_path_for, load_config, save_config
from lies.library.errors import CollectionNotFound
from lies.library.record import LibraryCollectionConfig


library_collections_app = typer.Typer(
    name="library",
    help="Inspect and author library-collection configs.",
    rich_help_panel="Library",
)


@library_collections_app.command("list")
def list_cmd(
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit JSON array of records."),
    ] = False,
) -> None:
    """List every library collection with its source and tags."""
    from lies.library.registry import library_collection_records

    records = list(library_collection_records())
    if json_output:
        rows = [
            {
                "name": r.name,
                "source": r.source,
                "tags": list(r.tags),
                "language": r.language,
            }
            for r in records
        ]
        typer.echo(json.dumps(rows, indent=2))
        return
    for r in records:
        typer.echo(r.name)


@library_collections_app.command("show")
def show_cmd(
    slug: Annotated[str, typer.Argument(help="Collection slug.")],
) -> None:
    """Show a single collection's full config."""
    try:
        rec = load_config(slug)
    except CollectionNotFound as exc:
        raise typer.BadParameter(str(exc))
    typer.echo(f"name={rec.name} source={rec.source} tags={list(rec.tags)}")
    typer.echo(f"language: {rec.language}")


@library_collections_app.command("where")
def where_cmd(
    slug: Annotated[str, typer.Argument(help="Collection slug.")],
) -> None:
    """Print the on-disk config path for ``slug``."""
    typer.echo(str(config_path_for(slug)))


@library_collections_app.command("new")
def new_cmd(
    slug: Annotated[str, typer.Argument(help="Collection slug to create.")],
    *,
    source: Annotated[str | None, typer.Option(help="Source URL or path.")] = None,
    prompt: Annotated[str | None, typer.Option(help="Prompt file (wizard mode).")] = None,
    tag: Annotated[list[str] | None, typer.Option("--tag", help="Tag (repeatable).")] = None,
) -> None:
    """Create a new collection config.

    The config is always persisted to disk on success; ``bootstrap_library_collection``
    is the single source of truth for "did this get written?".
    """
    from lies.library.bootstrap import bootstrap_library_collection

    if not source:
        raise typer.BadParameter("library new requires --source")
    rec = bootstrap_library_collection(slug, source, wizard=bool(prompt))
    if tag:
        rec = LibraryCollectionConfig(
            name=rec.name,
            source=rec.source,
            tags=tuple(dict.fromkeys([*rec.tags, *tag])),
            scraper_cmd=rec.scraper_cmd,
            doc_path=rec.doc_path,
            mapper_model=rec.mapper_model,
            language=rec.language,
            version=rec.version,
            created_at=rec.created_at,
            updated_at=datetime.now(tz=UTC),
            config=dict(rec.config),
        )
        save_config(rec, force=True)
    typer.echo(f"wrote {config_path_for(slug)}")


@library_collections_app.command("modify")
def modify_cmd(
    slug: Annotated[str, typer.Argument(help="Collection slug to modify.")],
    *,
    set_: Annotated[list[str] | None, typer.Option("--set", help="KEY=VALUE (repeatable).")] = None,
    tag: Annotated[list[str] | None, typer.Option("--tag", help="Tag to add (repeatable).")] = None,
    untag: Annotated[
        list[str] | None, typer.Option("--untag", help="Tag to remove (repeatable).")
    ] = None,
    from_file: Annotated[Path | None, typer.Option("--from-file", help="YAML patch file.")] = None,
) -> None:
    """Mutate an existing collection's source, tags, or other fields."""
    import yaml  # type: ignore[import-untyped]
    from dataclasses import replace as _dc_replace

    rec = load_config(slug)
    editable = {"source", "tags", "scraper_cmd", "doc_path", "mapper_model", "language", "config"}
    updates: dict[str, Any] = {}

    if from_file is not None and set_:
        raise typer.BadParameter("modify accepts --from-file or --set, not both")
    if from_file is not None:
        if not from_file.exists():
            raise typer.BadParameter(f"--from-file not found: {from_file}")
        try:
            payload = yaml.safe_load(from_file.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise typer.BadParameter(f"--from-file invalid YAML: {exc}") from exc
        if not isinstance(payload, dict):
            raise typer.BadParameter("--from-file root must be a mapping")
        # YAML `config:` with no value, or explicit `null`, becomes None;
        # the schema's default_factory expects `{}`.
        if "config" in payload and payload["config"] is None:
            payload["config"] = {}
        for k in payload:
            if k not in editable:
                raise typer.BadParameter(f"--from-file key {k!r} not editable")
        updates.update(payload)
    if set_:
        for raw in set_:
            if "=" not in raw:
                raise typer.BadParameter(f"--set expects KEY=VALUE, got {raw!r}")
            key, value = raw.split("=", 1)
            key, value = key.strip(), value.strip()
            if key.startswith("config."):
                subkey = key[len("config.") :]
                if not subkey:
                    raise typer.BadParameter(f"--set config. requires a subkey; got {raw!r}")
                parsed: Any = yaml.safe_load(value)
                updates.setdefault("config", {})[subkey] = parsed
                continue
            if key not in editable:
                raise typer.BadParameter(
                    f"key {key!r} not editable; allowed: {sorted(editable)} "
                    "or dotted keys like 'config.<subkey>'"
                )
            if key == "tags":
                updates["tags"] = [p for p in (s.strip() for s in value.split(",")) if p]
            elif key == "doc_path":
                updates["doc_path"] = Path(value)
            else:
                updates[key] = value

    cur_tags = list(updates.get("tags", list(rec.tags)))
    if tag:
        for t in tag:
            if t and t not in cur_tags:
                cur_tags.append(t)
    if untag:
        cur_tags = [t for t in cur_tags if t not in set(untag)]
    if tag or untag:
        updates["tags"] = cur_tags
    updates["updated_at"] = datetime.now(tz=UTC)

    new = _dc_replace(rec, **updates)
    save_config(new, force=True)
    typer.echo(f"updated {config_path_for(slug)}")


@library_collections_app.command("delete")
def delete_cmd(
    slug: Annotated[str, typer.Argument(help="Collection slug to delete.")],
    *,
    force: Annotated[bool, typer.Option("--force", help="Skip confirmation.")] = False,
) -> None:
    """Delete a collection's config YAML (does not touch scraped content)."""
    from rich.prompt import Confirm

    path = config_path_for(slug)
    if not path.exists():
        raise typer.BadParameter(f"collection {slug!r} not found at {path}")
    if not force and not Confirm.ask(f"Delete {path}?", default=False):
        typer.echo("aborted")
        raise typer.Exit(code=0)
    path.unlink()
    # Remove the now-empty per-slug directory so a future `library new` does
    # not collide with the ghost container.
    parent = path.parent
    with contextlib.suppress(OSError):
        if parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
    typer.echo(f"deleted {path}")


@library_collections_app.command("enrich-tags")
def enrich_tags_cmd() -> None:
    """Print hints for collections with empty tags.

    Dry-run only; the operator runs the printed ``lies library modify``
    commands by hand. Reserved for a future auto-apply.
    """
    from lies.library.registry import library_collection_records

    for rec in library_collection_records():
        if rec.tags:
            continue
        typer.echo(f"lies library modify {rec.name} --set tags=<comma-separated>")
