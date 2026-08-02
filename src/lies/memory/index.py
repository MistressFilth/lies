"""Deterministic index rebuild and log append.

``rebuild_index`` walks the wiki, groups pages by page type, and emits
a stable index body. ``append_log_entry`` records one parseable
``## [YYYY-MM-DD] <op> | <title>`` line per memory operation.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from lies.memory.validation import ALLOWED_PAGE_TYPES, parse_frontmatter
from lies.wiki.layout import WikiLayout

_PAGE_FILENAME_RE = re.compile(r"^(?P<name>.+)\.md$")


def _page_type_dir(path: Path) -> str:
    """Return the page-type subdirectory name (concepts, entities, ...)."""
    return path.parent.name


def _page_title_from_frontmatter(content: str, fallback: str) -> str:
    metadata = parse_frontmatter(content)
    title = metadata.get("title")
    if isinstance(title, str) and title.strip():
        return title
    return fallback


def _discover_pages(layout: WikiLayout) -> dict[str, list[tuple[str, str, str]]]:
    """Return a mapping of page_type -> [(title, path_posix, name)]."""
    grouped: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    if not layout.wiki_dir.exists():
        return grouped
    for path in sorted(layout.wiki_dir.rglob("*.md")):
        rel = path.relative_to(layout.root).as_posix()
        if rel in {
            "wiki/index.md",
            "wiki/log.md",
            "wiki/overview.md",
            "wiki/lint-report.md",
        }:
            continue
        match = _PAGE_FILENAME_RE.search(path.name)
        if match is None:
            continue
        name = match.group("name")
        page_type = _page_type_dir(path)
        normalized_type = (
            page_type[:-3] + "y" if page_type.endswith("ies") else page_type.removesuffix("s")
        )
        if page_type not in ALLOWED_PAGE_TYPES and normalized_type not in ALLOWED_PAGE_TYPES:
            continue

        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        title = _page_title_from_frontmatter(content, fallback=name)
        grouped[page_type].append((title, rel, name))
    for entries in grouped.values():
        entries.sort(key=lambda e: e[0].lower())
    return grouped


def rebuild_index(layout: WikiLayout) -> str:
    """Rebuild ``wiki/index.md`` and return its body."""
    grouped = _discover_pages(layout)
    today = datetime.now(timezone.utc).date().isoformat()
    lines = [
        "# Index",
        "",
        f"_Rebuilt {today}._",
        "",
    ]
    for page_type in sorted(grouped):
        if not grouped[page_type]:
            continue
        lines.append(f"## {page_type}")
        lines.append("")
        for title, rel, name in grouped[page_type]:
            lines.append(f"- [{title}]({rel}) — `{name}`")
        lines.append("")
    body = "\n".join(lines)
    layout.wiki_dir.mkdir(parents=True, exist_ok=True)
    layout.index_path.write_text(body, encoding="utf-8")
    return body


def append_log_entry(layout: WikiLayout, line: str) -> None:
    """Append a single parseable line to ``wiki/log.md``."""
    today = datetime.now(timezone.utc).date().isoformat()
    timestamped = line.rstrip("\n")
    if "{date}" in timestamped:
        timestamped = timestamped.replace("{date}", today)
    if not timestamped.startswith("## "):
        timestamped = f"## [{today}] {timestamped}"
    layout.wiki_dir.mkdir(parents=True, exist_ok=True)
    with layout.log_path.open("a", encoding="utf-8") as fh:
        fh.write(timestamped.rstrip() + "\n")
