"""Apply Obsidian conventions: frontmatter + tags + WikiLinks."""

from __future__ import annotations

from typing import Any

import yaml  # type: ignore[import-untyped]


def apply(markdown: str, *, frontmatter: dict[str, Any] | None = None) -> str:
    """Inject YAML frontmatter into a markdown string."""
    if not frontmatter:
        return markdown
    fm = yaml.safe_dump(frontmatter, sort_keys=True).strip()
    body = markdown.lstrip("\n")
    if body.startswith("---\n"):
        return body
    return f"---\n{fm}\n---\n\n{body}"
