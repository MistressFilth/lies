"""Env var override: ``LIES_<AGENT>_MODEL`` beats TOML when set to a non-empty string."""

from __future__ import annotations

import os


def env_override(agent_name: str) -> str | None:
    var = f"LIES_{agent_name.upper()}_MODEL"
    value = os.environ.get(var)
    return value if value else None
