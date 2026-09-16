"""Load + version-stamp the LIES MCP orientation payloads.

Reads ``instructions.md`` and ``prompts/*.md`` from disk and
substitutes ``{version}`` from :data:`lies.__version__` into the
rendered text. Used by :mod:`lies.mcp.server` at MCP server boot.

Tested by ``tests/mcp/test_instructions_loader.py``.
"""

from __future__ import annotations

from pathlib import Path

from lies import __version__

INSTRUCTIONS_PATH = Path(__file__).parent / "instructions.md"
PROMPTS_DIR = Path(__file__).parent / "prompts"


def load_instructions() -> str:
    """Return the rendered handshake ``instructions=`` payload.

    Raises ``FileNotFoundError`` if ``instructions.md`` is missing.
    """
    raw = INSTRUCTIONS_PATH.read_text(encoding="utf-8")
    return raw.format(version=__version__)


def load_prompt(name: str) -> str:
    """Return a prompt body by filename stem (no extension).

    Raises ``FileNotFoundError`` if ``prompts/<name>.md`` is missing.
    """
    path = PROMPTS_DIR / f"{name}.md"
    return path.read_text(encoding="utf-8").format(version=__version__)
