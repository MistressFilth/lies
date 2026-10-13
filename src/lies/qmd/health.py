"""Cheap reachability probe for the qmd HTTP daemon."""

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
    except TimeoutError, OSError:
        return False
