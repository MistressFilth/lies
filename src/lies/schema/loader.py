"""Load the schema for a wiki: per-wiki override or default.

Also exposes :func:`load_page` / :func:`dump_page` for round-tripping
individual wiki pages (frontmatter + body) through the wiki schema.
Frontmatter is parsed as a generic dict via ``python-frontmatter``;
no per-field Pydantic schema is enforced. The loader round-trips all
fields, including ``derived_from: list[str]`` for filed synthesis
pages, without value validation.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import frontmatter  # type: ignore[import-untyped]

from lies.wiki.wiki import Wiki


class SchemaNotFoundError(Exception):
    """Raised when neither a per-wiki override nor a default schema exists."""


@dataclass(frozen=True)
class ParsedPage:
    """A wiki page split into YAML frontmatter and a markdown body.

    Frontmatter is exposed as a generic dict; the loader does not
    enforce field schemas. Custom fields such as ``derived_from`` (a
    ``list[str]`` of wiki-relative slugs used by synthesis pages)
    round-trip unchanged through :func:`dump_page`.
    """

    frontmatter: dict[str, Any]
    body: str


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
    except (FileNotFoundError, ModuleNotFoundError):
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


def load_page(path: Path) -> ParsedPage:
    """Read ``path`` and return its frontmatter + body as a :class:`ParsedPage`.

    Frontmatter is parsed as a generic dict; ``python-frontmatter``
    handles the YAML delimiter and yields a flat mapping. Custom
    fields (e.g. ``derived_from``) flow through unchanged.
    """
    content = path.read_text(encoding="utf-8")
    post = frontmatter.loads(content)
    return ParsedPage(frontmatter=dict(post.metadata or {}), body=post.content)


def dump_page(parsed: ParsedPage, path: Path) -> str:
    """Serialize ``parsed`` to ``path`` and return the dumped markdown.

    The serialized form preserves the frontmatter dict (including
    list fields such as ``derived_from: list[str]``) and the body.
    """
    post = frontmatter.Post(parsed.body, **parsed.frontmatter)
    dumped = frontmatter.dumps(post)
    path.write_text(dumped, encoding="utf-8")
    return dumped
