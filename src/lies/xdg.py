"""XDG Base Directory path resolution.

Reads ``LIES_XDG_<NAME>`` first, falls back to the spec env var
(``XDG_DATA_HOME`` etc.), then the spec default. ``runtime_dir()`` is
best-effort: if ``XDG_RUNTIME_DIR`` is unset the runtime root falls
under the state home (per-wiki subdir) so tools that need a runtime
location on systems without one still get one.
"""

from __future__ import annotations

import os
from pathlib import Path

_HOME = Path.home

_SPEC_DEFAULTS: dict[str, str] = {
    "XDG_DATA_HOME": "~/.local/share",
    "XDG_CONFIG_HOME": "~/.config",
    "XDG_CACHE_HOME": "~/.cache",
    "XDG_STATE_HOME": "~/.local/state",
}

_LIES_OVERRIDE = {
    "XDG_DATA_HOME": "LIES_XDG_DATA_HOME",
    "XDG_CONFIG_HOME": "LIES_XDG_CONFIG_HOME",
    "XDG_CACHE_HOME": "LIES_XDG_CACHE_HOME",
    "XDG_STATE_HOME": "LIES_XDG_STATE_HOME",
    "XDG_RUNTIME_DIR": "LIES_XDG_RUNTIME_DIR",
}


def _xdg_root(env_var: str, default: str | None) -> Path:
    override = _LIES_OVERRIDE.get(env_var)
    if override:
        value = os.environ.get(override)
        if value:
            return Path(value).expanduser()
    value = os.environ.get(env_var)
    if value:
        return Path(value).expanduser()
    if default is None:
        # Caller is responsible for fallback (runtime_dir handles XDG_RUNTIME_DIR).
        msg = f"required XDG env var unset: {env_var}"
        raise RuntimeError(msg)
    return Path(default).expanduser()


def data_home() -> Path:
    return _xdg_root("XDG_DATA_HOME", _SPEC_DEFAULTS["XDG_DATA_HOME"])


def config_home() -> Path:
    return _xdg_root("XDG_CONFIG_HOME", _SPEC_DEFAULTS["XDG_CONFIG_HOME"])


def cache_home() -> Path:
    return _xdg_root("XDG_CACHE_HOME", _SPEC_DEFAULTS["XDG_CACHE_HOME"])


def state_home() -> Path:
    return _xdg_root("XDG_STATE_HOME", _SPEC_DEFAULTS["XDG_STATE_HOME"])


def runtime_dir() -> Path:
    """Per-system runtime dir.

    Reads ``LIES_XDG_RUNTIME_DIR`` first (LIES-specific override), falls
    back to ``XDG_RUNTIME_DIR`` (spec env var, mkdir if missing), then to
    ``<state_home>/run`` when neither is set.
    """
    lies_override = os.environ.get("LIES_XDG_RUNTIME_DIR")
    if lies_override:
        path = Path(lies_override).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        return path
    explicit = os.environ.get("XDG_RUNTIME_DIR")
    if explicit:
        path = Path(explicit).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        return path
    return state_home() / "run"


def runtime_dir_for(wiki: str) -> Path:
    """Per-wiki runtime dir under the system runtime dir."""
    return runtime_dir() / "lies" / wiki
