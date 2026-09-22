"""Chart render: extract the longest ```mermaid``` fence from the body.

Pass-through policy (F1 chart addendum): no validator, no retry. The
synthesizer owns shape; the renderer owns "give me the diagram, drop
the wrapper." Zero ```mermaid``` blocks → return body unchanged so the
operator sees the original prose; the CLI emits the stderr warning.
"""

from __future__ import annotations

import re

# Accept the standard fence shape with optional trailing whitespace
# after the ``mermaid`` lang tag (Markdown spec permits
# `` ```mermaid `` with a trailing space or tab). The closing fence
# must sit on its own line; bodies with inline fences after prose are
# not fences per spec and are not matched.
#
# Info-string strictness: the regex rejects fences that carry any
# CommonMark info string after ``mermaid`` (e.g. ``\`\`\`mermaid
# style=plain``). Synth output is expected to emit bare ``mermaid``
# fences per the chart-variant prompt's "EXACTLY ONE ```mermaid```
# fence" contract; non-empty info strings are a malformed-output
# signal we deliberately surface as a non-match (the CLI then emits
# the body unchanged with a stderr warning). Widening the regex to
# accept arbitrary info strings would mask the malformed-output
# signal — the current strict posture is intentional.
_MERMAID_FENCE = re.compile(
    r"```mermaid[ \t]*\n(.*?)\n```",
    re.DOTALL,
)


def render_chart(body: str) -> str:
    """Return the longest ```mermaid``` block from ``body``.

    Zero blocks → return ``body`` unchanged (pass-through; operator
    sees the original prose and the CLI emits a stderr warning).
    One+ blocks → return the block body (between fences) of the
    longest block. Empty body returns ``""``.

    Longest-wins rationale: the chart-variant prompt instructs
    ````EXACTLY ONE ```mermaid``` fence```` so multi-block output is
    malformed; the renderer silently picks the longest block rather
    than raising or asking the operator to disambiguate. This trades
    fail-loud for fail-useful — the operator still gets a renderable
    diagram from a confused synthesizer — but the malformed-output
    signal is lost. The CLI's stderr warning branch only fires when
    zero blocks are present; multi-block output produces no stderr
    signal today.
    """
    blocks = _MERMAID_FENCE.findall(body)
    if not blocks:
        return body
    return max(blocks, key=len)


__all__ = ("render_chart",)
