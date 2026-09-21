"""Chart render: extract the longest ```mermaid``` fence from the body.

Pass-through policy (F1 chart addendum): no validator, no retry. The
synthesizer owns shape; the renderer owns "give me the diagram, drop
the wrapper." Zero ```mermaid``` blocks → return body unchanged so the
operator sees the original prose; the CLI emits the stderr warning.
"""

from __future__ import annotations

import re

_MERMAID_FENCE = re.compile(
    r"```mermaid\s*\n(.*?)\n```",
    re.DOTALL,
)


def render_chart(body: str) -> str:
    """Return the longest ```mermaid``` block from ``body``.

    Zero blocks → return ``body`` unchanged (pass-through).
    One+ blocks → return the block body (between fences) of the longest.
    Empty body → return ``""``.
    """
    blocks = _MERMAID_FENCE.findall(body)
    if not blocks:
        return body
    return max(blocks, key=len)


__all__ = ("render_chart",)
