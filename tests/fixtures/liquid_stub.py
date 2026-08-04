"""Test-only render command for LiquidBuilder tests.

Used by `tests.fixtures.liquid_stub:render` lookups. The real
shopify-cli integration lives outside the repo; this stub lets unit
tests assert the resolver wiring without depending on Node + Ruby.
"""

from __future__ import annotations

# Constants for non-callable-attribute tests.
NON_CALLABLE = 42


def render(template_bytes: bytes, context: dict) -> bytes:
    """Stub renderer: returns HTML wrapping the template body.

    Real renderers (shopify-cli, python-liquid) replace this. The
    signature is contract: `(template_bytes, context) -> bytes`.
    """
    body = template_bytes.decode("utf-8", errors="replace")
    return f"<html><body>{body}</body></html>".encode()
