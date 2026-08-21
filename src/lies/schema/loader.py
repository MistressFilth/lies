"""Load the schema for a wiki: per-wiki override or default."""

from __future__ import annotations

import sys
from importlib import resources

from lies.wiki.wiki import Wiki


class SchemaNotFoundError(Exception):
    """Raised when neither a per-wiki override nor a default schema exists."""


def load_default_schema() -> str:
    """Return the default schema markdown shipped with LIES.

    Returns:
        The default schema text.

    Raises:
        SchemaNotFoundError: If the bundled default schema cannot be located.
    """
    try:
        return (
            resources.files("lies.schema").joinpath("default_schema.md").read_text(encoding="utf-8")
        )
    except FileNotFoundError, ModuleNotFoundError:
        raise SchemaNotFoundError(
            "Default schema not found in package (expected lies.schema.default_schema.md)"
        ) from sys.exc_info()[1]


def load_schema(wiki: Wiki) -> str:
    """Return the schema markdown for ``wiki``.

    Resolution order:
    1. ``wiki.schema_path`` (per-wiki override, under
       ``$XDG_CONFIG_HOME/lies/<name>/schema.md``)
    2. ``src/lies/schema/default_schema.md`` (default, shipped with LIES)

    Returns:
        The schema markdown text.
    """
    if wiki.schema_path.exists():
        return wiki.schema_path.read_text(encoding="utf-8")
    return load_default_schema()
