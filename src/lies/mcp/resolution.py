"""Wiki-root resolution and path-traversal guards.

`WikiRootError` is the one named exception raised by MCP tool bodies
when the requested wiki is missing, malformed, or escapes an allowed
boundary. Resolution follows the chain:

    explicit wiki_root arg → LIES_WIKI_ROOT env → Path.cwd()

`require_wiki=True` (the default) additionally requires the resolved
directory to contain a `wiki/` or `.lies/` subdirectory. Pass
`require_wiki=False` from `init_wiki` since the path is bootstrapped,
not pre-existing.
"""
from __future__ import annotations

import os
from pathlib import Path

from lies.wiki.layout import WikiLayout


class WikiRootError(ValueError):
    """Raised when a wiki root is missing, not a directory, or not a wiki."""


def _resolve_wiki_root(
    wiki_root: str | None,
    *,
    require_wiki: bool = True,
) -> WikiLayout:
    """Resolve ``wiki_root`` and return a ``WikiLayout``.

    Resolution order: explicit arg → ``LIES_WIKI_ROOT`` env → cwd.
    The resolved path must exist and be a directory. When
    ``require_wiki`` is True (default), the directory must contain a
    ``wiki/`` or ``.lies/`` subdirectory.
    """
    raw: str | None
    if wiki_root is not None and wiki_root != "":
        raw = wiki_root
    elif env := os.environ.get("LIES_WIKI_ROOT"):
        raw = env
    else:
        raw = "."

    path = Path(raw).expanduser().resolve()

    if not path.exists():
        raise WikiRootError(f"wiki root does not exist: {path}")
    if not path.is_dir():
        raise WikiRootError(f"wiki root is not a directory: {path}")

    if require_wiki:
        has_wiki = (path / "wiki").is_dir()
        has_lies = (path / ".lies").is_dir()
        if not (has_wiki or has_lies):
            raise WikiRootError(
                f"wiki root has no wiki layout (no wiki/ or .lies/): {path}"
            )

    return WikiLayout(path)


def _safe_page_path(wiki_root: Path, page: str) -> Path:
    """Resolve ``page`` against ``<wiki_root>/wiki/`` or reject.

    Rejects:
      - absolute paths
      - paths containing ``..`` components that escape wiki/
      - paths that resolve outside ``wiki_root/wiki/``

    Returns the resolved absolute ``Path``. The file need not exist
    (resources return empty string for missing files).
    """
    if not page:
        raise WikiRootError("page path is empty")
    p = Path(page)
    if p.is_absolute():
        raise WikiRootError(f"page path must be relative, got absolute: {page}")

    wiki_dir = (wiki_root / "wiki").resolve()
    resolved = (wiki_dir / p).resolve()

    try:
        resolved.relative_to(wiki_dir)
    except ValueError as exc:
        raise WikiRootError(
            f"page path escapes wiki root: {page}"
        ) from exc

    return resolved
