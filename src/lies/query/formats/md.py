"""Markdown render: identity with trailing-newline normalization."""

from __future__ import annotations


def render_markdown(body: str) -> str:
    """Return ``body`` with a single trailing newline.

    Empty body returns empty (don't fabricate a newline when the source
    body is empty). Non-empty body always ends with a single ``\\n``.

    The CLI's ``rich.markdown.Markdown`` renderer handles the rest.
    """
    if not body:
        return ""
    return body if body.endswith("\n") else body + "\n"


__all__ = ("render_markdown",)
