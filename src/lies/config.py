"""Runtime configuration: env vars, model selection, paths."""

from __future__ import annotations

import os
from pathlib import Path

from lies import xdg

DEFAULT_QMD_TRANSPORT = "http"
DEFAULT_QMD_URL = "http://127.0.0.1:8181"
DEFAULT_WIKI_NAME = "default"
# Per-call deadline for a qmd *retrieval* subprocess. Read at call
# time, not import time, so a test (or an operator inside one process)
# can move it without reloading modules.
#
# 60s is ``qmd_query``'s own signature default, so the retrieval layer
# is no longer stricter than the layer beneath it. The previous 15s was
# sized in ``grounding`` on a 2026-09-25 cold-daemon probe (~3-7s warm,
# cold rerank can pass 10s) and inherited by ``search`` by copy, along
# with neither its rationale nor its env override. Live measurement
# against the 5987-doc corpus (2026-10-01): warm 5.6-6.0s, three
# concurrent clients 5.9-6.6s, no timeouts in ~150 calls; intermittent
# stalls past 15s occur under host contention and the trigger was not
# isolated.
#
# This governs *retrieval only*. The liveness probes in
# ``qmd.lifecycle`` / ``qmd.daemon`` keep their own short deadlines —
# a slow answer to "is this alive?" is itself the failure, and a
# wedged daemon should be reported rather than waited on.
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
    """Per-call deadline, in seconds, for a qmd retrieval subprocess.

    The single source for every query call site. It lived as a
    hardcoded literal in ``mcp/search.py`` and as a second literal in
    ``mcp/grounding.py`` — one env var, two different answers, so the
    knob was coherent only when set. The variable keeps its historical
    name (``LIES_QMD_FANOUT_TIMEOUT``) because it has been the fan-out's
    override since 0.40.0 and renaming it would break an operator's
    muscle memory for no gain.

    A malformed value falls back to the default rather than raising:
    this is read on the retrieval path, and an operator typo should
    cost the default budget, not the search.
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
