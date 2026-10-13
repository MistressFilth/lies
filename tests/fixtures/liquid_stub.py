"""Test-only render command for LiquidBuilder tests."""

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


def raise_render(template_bytes: bytes, context: dict) -> bytes:
    """Always fails; used to test quarantine behaviour."""
    raise RuntimeError("simulated renderer failure")


def string_render(template_bytes: bytes, context: dict) -> str:
    """Returns a non-bytes value; should be rejected."""
    return "<html>nope</html>"
