"""Load the schema for a wiki: per-wiki override or default.

Also exposes :func:`load_page` / :func:`dump_page` for round-tripping
individual wiki pages (frontmatter + body) through the wiki schema.
Frontmatter is parsed as a generic dict via ``python-frontmatter``;
no per-field Pydantic schema is enforced. The loader round-trips all
fields, including ``derived_from: list[str]`` for filed synthesis
pages, without value validation.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import frontmatter  # type: ignore[import-untyped]

from lies.schema.sections import SectionContract
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


class SchemaSectionContractInvalid(ValueError):
    """Raised when the ``## Section contract`` block is malformed."""


_VALID_SECTION_TYPES = frozenset(SectionContract.model_fields.keys())

# - **<type>** — `## <heading>`, `## <heading>`, ...
_SECTION_ITEM_RE = re.compile(
    r"^\s*-\s+\*\*(\w+)\*\*\s+—\s+(.+?)\s*$",
    re.MULTILINE,
)

# `## <heading>` (greedy until backtick)
_SECTION_HEADING_RE = re.compile(r"`##\s*([^`]+?)`")

_SECTION_BLOCK_RE = re.compile(
    r"^##\s+Section contract\s*\n(.*?)(?=^##\s|\Z)",
    re.MULTILINE | re.DOTALL,
)


def parse_section_contract(markdown: str) -> SectionContract:
    """Parse the ``## Section contract`` block from a schema doc.

    Returns an empty :class:`SectionContract` when the block is absent.
    Raises :class:`SchemaSectionContractInvalid` on malformed input:
    unknown type, duplicate type, missing backticks, or empty heading.

    When two ``## Section contract`` blocks exist, the second wins.
    Independent of the existing ``derived_from`` round-trip.
    """
    matches = list(_SECTION_BLOCK_RE.finditer(markdown))
    if not matches:
        return SectionContract()
    block = matches[-1].group(1)
    fields: dict[str, list[str]] = {t: [] for t in _VALID_SECTION_TYPES}
    for item_match in _SECTION_ITEM_RE.finditer(block):
        type_name = item_match.group(1)
        rest = item_match.group(2)
        if type_name not in _VALID_SECTION_TYPES:
            raise SchemaSectionContractInvalid(
                f"unknown page type '{type_name}' in Section contract; "
                f"expected one of {sorted(_VALID_SECTION_TYPES)}"
            )
        if fields[type_name]:
            raise SchemaSectionContractInvalid(
                f"duplicate page type '{type_name}' in Section contract"
            )
        # Allow (none) as the "no required sections" sentinel.
        if rest.strip() == "(none)":
            continue
        headings = _SECTION_HEADING_RE.findall(rest)
        if not headings:
            raise SchemaSectionContractInvalid(
                f"page type '{type_name}' has no `## <Heading>` tokens; "
                f"use '(none)' to declare zero required sections"
            )
        for raw in headings:
            heading = raw.strip()
            if not heading:
                raise SchemaSectionContractInvalid(f"empty heading in page type '{type_name}'")
            fields[type_name].append(f"## {heading}")
    return SectionContract(**fields)
