"""Mirror file writer: combine slug + frontmatter + body to disk."""

from __future__ import annotations

from pathlib import Path

from lies.library.frontmatter import build_frontmatter
from lies.library.paths import LibraryCollection
from lies.library.slug import validate_slug


def render_mirror(
    *,
    slug: str,
    body: str,
    source_url: str | None,
    source_path: str | None,
    source_hash: str,
    fetched_via: str,
    title: str | None = None,
) -> str:
    if title is None:
        title = slug.replace("-", " ").title()
    fm = build_frontmatter(
        title=title,
        source_url=source_url,
        source_path=source_path,
        source_hash=source_hash,
        fetched_via=fetched_via,
    )
    body = body if body.endswith("\n") else body + "\n"
    return fm + "\n" + body


def write_mirror(
    collection: LibraryCollection,
    slug: str,
    body: str,
    *,
    source_url: str | None = None,
    source_path: str | None = None,
    source_hash: str,
    fetched_via: str,
    title: str | None = None,
    force: bool = False,
) -> Path:
    validate_slug(slug)
    target = collection.dir / f"{slug}.md"
    if target.exists() and not force:
        raise FileExistsError(f"mirror already exists: {target}")
    collection.dir.mkdir(parents=True, exist_ok=True)
    text = render_mirror(
        slug=slug,
        body=body,
        source_url=source_url,
        source_path=source_path,
        source_hash=source_hash,
        fetched_via=fetched_via,
        title=title,
    )
    target.write_text(text, encoding="utf-8")
    return target


__all__ = ("render_mirror", "write_mirror")
