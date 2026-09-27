"""Load + version-stamp the LIES MCP orientation payload.

Reads ``instructions.md`` from disk and substitutes ``$version``
from :data:`lies.__version__` into the rendered text via
:class:`string.Template`. ``string.Template`` uses ``$variable``
(or ``${variable}``) so literal ``{...}`` tokens in the source
file — e.g. the ``{slug}`` URI-template placeholders some
downstream sections reference — render unchanged.

The v0.40 rewrite retires the seven prompts the prior loader
also served (``answer`` / ``orient`` / ``ingest`` / ``lint`` /
``sync`` / ``file-back`` / ``cite``). ``PROMPTS_DIR`` and
``load_prompt`` are gone; ``instructions.md`` is the only
hand-edited orientation surface.

Used by :mod:`lies.mcp.server` at MCP server boot.
Tested by ``tests/unit/mcp/test_instructions_loader.py``.
"""

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
