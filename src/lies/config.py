"""Runtime configuration: env vars, model selection, paths."""

from __future__ import annotations

import os
from pathlib import Path

from lies import xdg

DEFAULT_QMD_TRANSPORT = "http"
DEFAULT_QMD_URL = "http://127.0.0.1:8181"
DEFAULT_WIKI_NAME = "default"


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


def get_qmd_url() -> str:
    return os.environ.get("LIES_QMD_URL", DEFAULT_QMD_URL)
