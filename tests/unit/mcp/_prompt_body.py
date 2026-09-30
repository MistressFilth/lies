"""Helper to extract the rendered body string from a FastMCP ``Message``.

The prompt test files all need to assert on the rendered body string.
A FastMCP ``Message`` carries its text on ``message.content.text``
(``content`` is a ``TextContent`` model). Older shapes exposed a
top-level ``.text``; this helper handles both and falls back to
``str(message)`` so the tests read clean either way.
"""

from __future__ import annotations


def rendered_body(message: object) -> str:
    """Return the rendered body of a FastMCP ``Message``.

    Prefers ``message.content.text``, then ``message.text``, then
    falls back to ``str(message)``.
    """
    content = getattr(message, "content", None)
    text = getattr(content, "text", None)
    if isinstance(text, str):
        return text
    top_level = getattr(message, "text", None)
    if isinstance(top_level, str):
        return top_level
    return str(message)


def carries_verbatim(body: str, value: str) -> bool:
    """Whether ``body`` carries ``value`` as a fenced verbatim block.

    ``ask`` and ``ground`` render the user question into a fenced block
    rather than a ``repr()`` literal, so a question carrying a quote or
    a newline reaches the tool call exactly as typed instead of
    arriving with escape sequences the agent has to know to strip.
    Matching the fence is what pins that: it fails for both a missing
    question and a repr-escaped one.
    """
    return f"\n{value}\n```" in body
