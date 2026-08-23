"""Core commands: version, init, migrate-xdg, config.

These commands don't talk to the model -- they only need cheap utilities.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from lies import __version__, xdg
from lies.cli import app
from lies.cli._helpers import LIES_DATA_SUBDIR, _emit_missing_providers_hint
from lies.wiki_settings import resolve_language

__all__ = (
    "config_cmd",
    "init",
    "migrate_xdg",
    "version",
)


@app.command(
    short_help="Print the LIES version and exit.",
    rich_help_panel="Meta",
)
def version() -> None:
    """Print the LIES version and exit."""
    typer.echo(f"lies {__version__}")


@app.command(
    short_help="Migrate a legacy .lies/ wiki into XDG role-routed dirs.",
    rich_help_panel="Wiki management",
)
def migrate_xdg(
    legacy_path: Annotated[
        Path, typer.Argument(help="Path to the legacy .lies/ directory to migrate.")
    ],
    name: Annotated[
        str, typer.Argument(help="Wiki name (the XDG role-routed subdir to migrate into).")
    ],
    force: Annotated[
        bool,
        typer.Option(
            "--force/--no-force",
            help="Overwrite existing files on the destination wiki (default: skip).",
        ),
    ] = False,
) -> None:
    """Migrate a legacy ``.lies/`` wiki into XDG role-routed dirs.

    With ``--force``, conflicting source files (destination has
    byte-mismatched content) are *quarantined* to
    ``<legacy_path>/.xdg-migration-conflicts/`` rather than dropped; the
    destination file is left untouched. Conflicts abort the migration by
    default so the caller can resolve them.
    """
    from lies.migrate import migrate_wiki

    result = migrate_wiki(legacy_path, name=name, force=force)
    if result.removed_legacy:
        typer.echo(f"migrated wiki '{name}' from {legacy_path}")
    elif result.skipped:
        typer.echo(f"wiki '{name}' already migrated (no-op)")
    typer.echo(
        f"copied={len(result.copied)} skipped={len(result.skipped)} "
        f"conflicts={len(result.conflicts)} "
        f"quarantined={len(result.quarantined)}"
    )


@app.command(
    name="config",
    short_help="Print active model + wiki name + resolved language + per-agent model assignments.",
    rich_help_panel="Wiki management",
)
def config_cmd(
    name: str = typer.Option(
        "default",
        "--name",
        "-n",
        envvar="LIES_WIKI_NAME",
        help="Wiki to print config for (default: $LIES_WIKI_NAME, falls back to 'default').",
    ),
) -> None:
    """Print active model + wiki name + resolved language + per-agent model assignments."""
    from lies.cli import resolve_wiki
    from lies.providers import (
        AGENT_ROSTER,
        ProviderConfigError,
        load_providers_config,
        resolve_model,
    )

    wiki = resolve_wiki(name)
    _emit_missing_providers_hint(wiki.providers_path)
    typer.echo(f"wiki: {wiki.name}")
    typer.echo(f"language: {resolve_language(wiki)}")

    cfg = load_providers_config(wiki.providers_path)
    if cfg is None:
        typer.echo(f"model: (no providers.toml at {wiki.providers_path})")
        typer.echo("agent models: (none configured)")
        return

    typer.echo(f"model: {cfg.default_model}")
    typer.echo("agent models:")
    width = max(len(agent_name) for agent_name in AGENT_ROSTER)
    for agent_name in AGENT_ROSTER:
        try:
            resolved = resolve_model(agent_name, cfg)
        except ProviderConfigError as exc:
            typer.echo(f"  {agent_name.ljust(width)}  (unresolved: {exc})")
            continue
        if isinstance(resolved, str):
            typer.echo(f"  {agent_name.ljust(width)}  {resolved}")
        else:
            # Resolved into a Model instance (custom anthropic_compatible
            # provider); show the TOML string the user wrote -- which is
            # the model identifier, not the constructed client.
            typer.echo(f"  {agent_name.ljust(width)}  {cfg.agents[agent_name]}")


@app.command(
    short_help="Initialize a new wiki under XDG_DATA_HOME.",
    rich_help_panel="Wiki management",
)
def init(
    name: Annotated[
        str, typer.Argument(help="Wiki name (the XDG role-routed subdir, e.g. 'mywiki').")
    ],
) -> None:
    """Initialize a new wiki under XDG_DATA_HOME."""
    from lies.errors import WikiAlreadyExists
    from lies.wiki.layout import WikiLayout, copy_default_schema, git_init_initial
    from lies.wiki.wiki import Wiki

    wiki = Wiki(
        name=name,
        data_root=Wiki.data_root_for(name),
        config_root=xdg.config_home() / LIES_DATA_SUBDIR / name,
        cache_root=xdg.cache_home() / LIES_DATA_SUBDIR / name,
        state_root=xdg.state_home() / LIES_DATA_SUBDIR / name,
        runtime_root=xdg.runtime_dir_for(name),
    )
    if wiki.data_root.exists():
        typer.echo(f"error: {WikiAlreadyExists(name, wiki.data_root)}", err=True)
        raise typer.Exit(code=1)
    # All five role roots
    for root in (
        wiki.data_root,
        wiki.config_root,
        wiki.cache_root,
        wiki.state_root,
        wiki.runtime_root,
    ):
        root.mkdir(parents=True, exist_ok=True)
    # Known subdirs under config_root -- ready for users to drop YAMLs in.
    wiki.collections_dir.mkdir(parents=True, exist_ok=True)
    WikiLayout(wiki.data_root).init()
    copy_default_schema(wiki.schema_path)
    git_init_initial(wiki.data_root)
    _emit_missing_providers_hint(wiki.providers_path)
    typer.echo(f"initialized wiki '{name}' at {wiki.data_root}")
