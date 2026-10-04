"""Runtime configuration: env vars, model selection, paths."""

from __future__ import annotations

import os
from pathlib import Path

from lies import xdg

DEFAULT_QMD_TRANSPORT = "http"
# The path qmd's HTTP MCP server serves. A bare origin 404s, which
# the access seam reads as a transport failure and reports as a
# *down* daemon.
DEFAULT_QMD_URL = "http://127.0.0.1:8181/mcp"
DEFAULT_WIKI_NAME = "default"
# Per-call deadline for a qmd *retrieval* subprocess, read at call
# time. 60s is ``qmd_query``'s own signature default. Live
# measurement against the 5987-doc corpus (2026-10-01): warm
# 5.6-6.0s, three concurrent clients 5.9-6.6s, no timeouts in ~150
# calls; intermittent stalls past 15s occur under host contention.
# Liveness probes keep their own short deadlines.
DEFAULT_QMD_QUERY_TIMEOUT_S = 60


def get_wiki_name() -> str:
    return os.environ.get("LIES_WIKI_NAME", DEFAULT_WIKI_NAME)


def get_xdg_data_home() -> Path:
    return xdg.data_home()


def get_xdg_config_home() -> Path:
    return xdg.config_home()


def get_xdg_cache_home() -> Path:
    return xdg.cache_home()


def get_xdg_state_home() -> Path:
    return xdg.state_home()


def get_xdg_runtime_dir() -> Path:
    return xdg.runtime_dir()


def get_qmd_transport() -> str:
    return os.environ.get("LIES_QMD_TRANSPORT", DEFAULT_QMD_TRANSPORT)


def get_qmd_query_timeout() -> int:
    """Per-call deadline for a qmd retrieval subprocess (the single source).

    Variable name is ``LIES_QMD_FANOUT_TIMEOUT`` (historical, the
    fan-out's override since 0.40.0). A malformed value falls back
    to the default rather than raising.
    """
    raw = os.environ.get("LIES_QMD_FANOUT_TIMEOUT")
    if raw is None:
        return DEFAULT_QMD_QUERY_TIMEOUT_S
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_QMD_QUERY_TIMEOUT_S
    return value if value > 0 else DEFAULT_QMD_QUERY_TIMEOUT_S


def get_qmd_url() -> str:
    return os.environ.get("LIES_QMD_URL", DEFAULT_QMD_URL)
