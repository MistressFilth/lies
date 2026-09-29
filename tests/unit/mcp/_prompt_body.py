"""Helper to extract the rendered body string from a FastMCP ``Message``.

The 7 slash-prompt test files all need to assert on the rendered
body string. FastMCP exposes ``Message.text`` on the modern shape
and falls back to ``str(message)`` on the older shape. This helper
encapsulates that fallback so the tests read clean.
"""

from __future__ import annotations


def rendered_body(message: object) -> str:
    """Return the rendered body of a FastMCP ``Message``.

    Uses ``message.text`` when present (modern FastMCP); falls back
    to ``str(message)`` for older shapes.
    """
    return getattr(message, "text", None) or str(message)
