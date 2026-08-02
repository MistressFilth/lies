"""Load the schema for a wiki: per-wiki override or default."""

from __future__ import annotations

from importlib import resources

from lies.wiki.layout import WikiLayout


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
    except (FileNotFoundError, ModuleNotFoundError) as exc:
        raise SchemaNotFoundError(
            "Default schema not found in package (expected lies.schema.default_schema.md)"
        ) from exc


def load_schema(layout: WikiLayout) -> str:
    """Return the schema markdown for the wiki at `layout`.

    Resolution order:
    1. `<wiki>/.lies/schema.md` (per-wiki override)
    2. `src/lies/schema/default_schema.md` (default, shipped with LIES)

    Returns:
        The schema markdown text.
    """
    if layout.schema_path.exists():
        return layout.schema_path.read_text(encoding="utf-8")
    return load_default_schema()
