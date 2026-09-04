"""Catalog page + section model. SQLite row shape mirrors ask's `CatalogPage`."""

from __future__ import annotations

import hashlib
from datetime import date
from enum import Enum
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict


class PageSection(str, Enum):
    """Catalog section discriminator. Mirrors ask's CHECK constraint."""

    wiki = "wiki"
    ingested = "ingested"


class CatalogPage(BaseModel):
    """A single page in the wiki catalog. Frozen."""

    model_config = ConfigDict(frozen=True)

    slug: str
    title: str = ""
    type: str = ""
    source_pkg: str = ""
    section: PageSection = PageSection.wiki
    updated: str = ""  # ISO date; empty when unknown
    hash: str = ""
    derived_from: str = ""  # comma-joined slug list

    @classmethod
    def from_path(
        cls,
        wiki: object,  # lies.wiki.wiki.Wiki (avoid circular import)
        path: str,
        *,
        derived_from: str = "",
    ) -> CatalogPage:
        """Build a CatalogPage from a wiki-relative path.

        ``path`` may be a wiki-relative slug (``claude-code/concepts/hooks``)
        or a path with the ``wiki/`` prefix. Reads the file, parses YAML
        frontmatter, computes SHA-256 of the body, derives ``source_pkg``
        from the first path segment (the collection subdir per PR #39).

        Missing files fall back to ``date.today().isoformat()`` as updated
        with empty title/type/hash. Unparseable frontmatter falls back to
        the slug as title.
        """
        wiki_dir: Path = wiki.wiki_dir  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]
        # Strip leading wiki/ defensively (page-writer emits wiki/-prefixed paths).
        rel = path.removeprefix("wiki/").removeprefix("wiki/")
        slug = rel.removesuffix(".md") if rel.endswith(".md") else rel
        # Look up the on-disk file; tolerate the caller passing a bare slug
        # (without the ``.md`` suffix) by appending it when needed.
        file_path = wiki_dir / rel
        if not file_path.exists() and not rel.endswith(".md"):
            file_path = wiki_dir / (rel + ".md")
        source_pkg = slug.split("/", 1)[0] if "/" in slug else ""

        if file_path.exists():
            content = file_path.read_text(encoding="utf-8")
            parsed_title, parsed_type, parsed_updated = _parse_frontmatter(content)
            body_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            updated = parsed_updated or date.today().isoformat()  # noqa: DTZ011
            title = parsed_title or slug
            page_type = parsed_type
        else:
            updated = date.today().isoformat()  # noqa: DTZ011
            title = ""
            page_type = ""
            body_hash = ""

        return cls(
            slug=slug,
            title=title,
            type=page_type,
            source_pkg=source_pkg,
            section=PageSection.wiki,
            updated=updated,
            hash=body_hash,
            derived_from=derived_from,
        )


def _parse_frontmatter(content: str) -> tuple[str, str, str]:
    """Return (title, type, updated) from the first YAML frontmatter block.

    Tolerates unparseable input by returning three empty strings.
    """
    if not content.startswith("---"):
        return ("", "", "")
    # Start searching past the opening "---\n" (positions 0-3) so we find the
    # closing "\n---" rather than the opening one.
    end = content.find("\n---", 4)
    if end == -1:
        return ("", "", "")
    fm_text = content[4:end]
    try:
        loaded = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError:
        return ("", "", "")
    if not isinstance(loaded, dict):
        return ("", "", "")
    title = str(loaded.get("title", ""))
    page_type = str(loaded.get("type", ""))
    updated = str(loaded.get("updated", ""))
    return (title, page_type, updated)
