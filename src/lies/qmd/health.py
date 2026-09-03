"""Cheap reachability probe for the qmd HTTP daemon.

The agent's `QmdCapability` calls this at construction time to decide
whether to advertise the native HTTP MCP tool or the local FastMCP
fallback. The probe is deliberately a single TCP connect with a short
timeout — we do not speak the MCP protocol here, only ask "would a
connection succeed?".

A successful probe does NOT guarantee the daemon is healthy enough
to serve a real tool call; per-call MCP errors are still caught by
the fallback's tool implementation. The probe exists to keep the
*advertised* tool surface honest: if the daemon is clearly down at
startup, the agent should not see native qmd tools at all.
"""

from __future__ import annotations

import socket
from urllib.parse import urlparse


def qmd_daemon_reachable(url: str, *, timeout: float = 0.5) -> bool:
    """Return True if a TCP connect to ``url`` succeeds within ``timeout``."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if not parsed.hostname or parsed.port is None:
        return False
    try:
        with socket.create_connection((parsed.hostname, parsed.port), timeout=timeout):
            return True
    except (TimeoutError, OSError):
        return False
