"""Config reader — reads .true-spec/project/true-spec.toml with safe defaults."""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic_guidance._models import HooksConfig


def read_hooks_config(cwd: str) -> HooksConfig:
    """Read [hooks] section from .true-spec/project/true-spec.toml in *cwd*.

    Returns HooksConfig() defaults (all enforcement enabled) when *cwd* is
    empty, the TOML file is absent, or any parse error occurs.
    """
    if not cwd:
        return HooksConfig()
    toml_path = Path(cwd) / ".true-spec" / "project" / "true-spec.toml"
    if not toml_path.exists():
        return HooksConfig()
    try:
        with open(toml_path, "rb") as f:
            data = tomllib.load(f)
        hooks_section = data.get("hooks", {})
        if not isinstance(hooks_section, dict):
            return HooksConfig()
        overrides = {
            key: bool(hooks_section[key])
            for key in HooksConfig.model_fields
            if key in hooks_section
        }
        return HooksConfig(**overrides)
    except (tomllib.TOMLDecodeError, OSError):
        return HooksConfig()
