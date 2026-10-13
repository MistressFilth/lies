"""Load + version-stamp the LIES MCP orientation payload."""

from __future__ import annotations

import string
from pathlib import Path

from lies import __version__

INSTRUCTIONS_PATH = Path(__file__).parent / "instructions.md"


def load_instructions() -> str:
    """Return the rendered handshake ``instructions=`` payload.

    Raises ``FileNotFoundError`` if ``instructions.md`` is missing.
    """
    raw = INSTRUCTIONS_PATH.read_text(encoding="utf-8")
    return string.Template(raw).safe_substitute(version=__version__)
