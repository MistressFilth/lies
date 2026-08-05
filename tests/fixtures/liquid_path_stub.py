"""Path-loaded render command for LiquidBuilder tests."""

from __future__ import annotations

calls: list[tuple[bytes, dict]] = []


def render(template_bytes: bytes, context: dict) -> bytes:
    """Record the render call and return deterministic HTML."""
    calls.append((template_bytes, context))
    return b"<html><body>rendered from path</body></html>"
