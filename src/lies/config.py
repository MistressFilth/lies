"""Runtime configuration: env vars, model selection, paths."""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_MODEL = "anthropic:claude-opus-4-7"
DEFAULT_QMD_TRANSPORT = "http"
DEFAULT_QMD_URL = "http://127.0.0.1:8181"


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


def get_qmd_transport() -> str:
    """Return the transport used to reach qmd's MCP surface.

    Defaults to ``http`` so the agent talks to the shared qmd daemon
    instead of spawning a `qmd` subprocess per agent. Set
    ``LIES_QMD_TRANSPORT=stdio`` to opt back out.
    """
    return os.environ.get("LIES_QMD_TRANSPORT", DEFAULT_QMD_TRANSPORT)


def get_qmd_url() -> str:
    """Return the URL of qmd's http MCP daemon.

    Reads ``LIES_QMD_URL``. The default matches the fixed port qmd's own
    ``qmd mcp --http --daemon`` binds.
    """
    return os.environ.get("LIES_QMD_URL", DEFAULT_QMD_URL)
