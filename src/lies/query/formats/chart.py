"""Chart render: extract the longest ```mermaid``` fence from the body.

Pass-through policy (F1 chart addendum): no validator, no retry. The
synthesizer owns shape; the renderer owns "give me the diagram, drop
the wrapper." Zero ```mermaid``` blocks → return body unchanged so the
operator sees the original prose; the CLI emits the stderr warning.
"""

from __future__ import annotations

import re

# Standard fence shape, with optional trailing whitespace after the ``mermaid`` lang tag
# (the Markdown spec permits it); the closing fence must sit on its own line. Any
# CommonMark info string after ``mermaid`` is rejected: synth is expected to emit bare
# fences, so a non-empty one is a malformed-output signal, not something to accommodate.
_MERMAID_FENCE = re.compile(
    r"```mermaid[ \t]*\n(.*?)\n```",
    re.DOTALL,
)


def render_chart(body: str) -> str:
    """Return the longest ```mermaid``` block from ``body``."""
    blocks = _MERMAID_FENCE.findall(body)
    if not blocks:
        return body
    return max(blocks, key=len)


__all__ = ("render_chart",)
