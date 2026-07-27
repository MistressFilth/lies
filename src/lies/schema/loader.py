"""Load the schema for a wiki: per-wiki override or default."""
from __future__ import annotations

from importlib import resources

from lies.wiki.layout import WikiLayout


class SchemaNotFoundError(Exception):
    """Raised when neither a per-wiki override nor a default schema exists."""


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

    try:
        return resources.files("lies.schema").joinpath("default_schema.md").read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError) as exc:
        raise SchemaNotFoundError(
            f"No schema found at {layout.schema_path} and no default schema in package"
        ) from exc
