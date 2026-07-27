"""Parse wiki/index.md-style content into the page references it contains.

The index is the content-oriented catalog of the wiki. It contains
markdown links to every page, grouped by page type. This module is the
substrate for the qmd-fallback path: when qmd is unavailable or returns
no results, we parse the index and read the top-N referenced pages.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")

# Skip http(s):// links and pure fragments.
_URL_PREFIXES = ("http://", "https://", "mailto:", "tel:")


@dataclass(frozen=True)
class IndexLink:
    """A single markdown link parsed from the wiki index."""

    title: str
    """The link's display text (e.g., 'Postgres')."""

    path: str
    """The link target, normalized: no fragment, no query, wiki-relative,
    ending in `.md` (e.g., 'entities/postgres.md')."""


def parse_index_links(content: str) -> list[IndexLink]:
    """Parse markdown links out of an index.md-style content block.

    Links are returned in the order they appear in the content (which
    matches the indexer's grouping convention: type → alphabetical).
    Duplicate (title, path) pairs are collapsed.

    URLs and fragment-only links are skipped. Only relative paths ending
    in `.md` are returned (per the wiki's markdown-only convention).
    """
    out: list[IndexLink] = []
    seen: set[tuple[str, str]] = set()

    for match in _LINK_RE.finditer(content):
        title = match.group(1).strip()
        raw = match.group(2).strip()

        # Strip fragment / query
        clean = raw.split("#", 1)[0].split("?", 1)[0].strip()
        if not clean:
            continue

        if clean.startswith(_URL_PREFIXES):
            continue
        if clean.startswith(("/", "\\")):
            # Absolute paths inside the wiki are not expected; skip
            continue
        if not clean.endswith(".md"):
            continue

        key = (title, clean)
        if key in seen:
            continue
        seen.add(key)

        out.append(IndexLink(title=title, path=clean))

    return out
