"""Runtime configuration: env vars, model selection, paths."""
from __future__ import annotations

import os
from pathlib import Path

DEFAULT_MODEL = "anthropic:claude-opus-4-7"


def get_model() -> str:
    """Return the configured model identifier.

    Reads `LIES_MODEL` from the environment; falls back to `DEFAULT_MODEL`.
    """
    return os.environ.get("LIES_MODEL", DEFAULT_MODEL)


def get_wiki_root() -> Path:
    """Return the configured wiki root directory.

    Reads `LIES_WIKI_ROOT` from the environment. Defaults to the current
    working directory.
    """
    return Path(os.environ.get("LIES_WIKI_ROOT", ".")).resolve()