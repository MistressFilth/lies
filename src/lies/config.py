"""Runtime configuration: env vars, model selection, paths."""

from __future__ import annotations

import os
from pathlib import Path

from lies import xdg

DEFAULT_QMD_TRANSPORT = "http"
# The path qmd's HTTP MCP server serves. A bare origin 404s, which
# the access seam reads as a transport failure and reports as a
# *down* daemon.
#
# The host is the *name* ``localhost``, not a literal address: qmd
# binds the name (``dist/mcp/server.js``: ``listen(port,
# "localhost")``) and Node takes the first family the host resolves
# it to. An IPv6-first ``/etc/hosts`` puts the daemon on ``[::1]``
# and refuses the IPv4 dial — measured on the CI runner. Naming the
# host resolves whichever family qmd got.
DEFAULT_QMD_URL = "http://localhost:9000/mcp"
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


def get_qmd_port() -> int:
    """The port of the configured qmd daemon, derived from :func:`get_qmd_url`.

    Derived rather than held as its own default so the URL and the port
    cannot disagree: a second literal is how ``lies qmd status`` came to
    probe 8181 while the daemon it was asked about ran on 9000.

    A value with no port, or one that does not parse, falls back to the
    port in :data:`DEFAULT_QMD_URL` rather than raising — the same
    tolerance :func:`get_qmd_query_timeout` applies to a malformed one.
    """
    from urllib.parse import urlsplit

    for url in (get_qmd_url(), DEFAULT_QMD_URL):
        try:
            port = urlsplit(url).port
        except ValueError:
            continue
        if port:
            return port
    return 9000
