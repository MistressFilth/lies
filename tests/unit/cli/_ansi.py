"""Shared helpers for asserting against Typer+Rich rendered CLI output."""

from __future__ import annotations

import re

# CSI sequences: ESC [ params final-byte. Final byte is the entire
# 0x40-0x7E range per ECMA-48; ``[a-zA-Z]`` covers the common subset
# (SGR ``m``, cursor moves, line erasure, etc.).
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from ``text``."""
    return _ANSI_RE.sub("", text)
