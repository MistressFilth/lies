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
    source_url: str,
    source_path: str,
    source_hash: str,
    fetched_via: str,
    title: str | None = None,
) -> str:
    """Render the frontmatter + body block as a single string.

    Minor 38: ``source_url`` and ``source_path`` are required
    keyword-only arguments. The brief mandates them; the previous
    ``None`` defaults silently allowed callers to omit the upstream
    provenance and still write a mirror with ``source_path: null`` /
    ``source_url: null`` rows in the frontmatter — the operator then
    had no way to trace a mirror back to its source.
    """
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
    source_url: str,
    source_path: str,
    source_hash: str,
    fetched_via: str,
    title: str | None = None,
    force: bool = False,
) -> Path:
    """Write the mirror file at ``<collection>/<slug>.md``.

    Minor 38: ``source_url`` and ``source_path`` are required
    keyword-only. The migration path passed ``source_path=None`` and
    ``source_url=None`` because the wiki-side schema lacked those
    fields — the mirror writer accepted the None defaults. After the
    migration sets the deterministic placeholder
    ``source_path="migrated-from-wiki"``, the writer no longer needs
    the None fall-through; the type signature now enforces both.
    """
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
