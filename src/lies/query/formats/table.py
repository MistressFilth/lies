"""Table render: identity. GFM tables render natively in rich.markdown.Markdown."""

from __future__ import annotations


def render_table(body: str) -> str:
    """Return ``body`` unchanged. The CLI renders GFM tables via ``rich``."""
    return body


__all__ = ("render_table",)
