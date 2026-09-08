"""Linter models — GuidanceFinding, HooksConfig, RulesConfig."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class GuidanceFinding(BaseModel):
    """A single pydantic-guidance finding emitted by the PG linter."""

    model_config = ConfigDict(frozen=True)

    line: int
    col: int = 0
    code: str
    message: str


class HooksConfig(BaseModel):
    """Typed configuration for the [hooks] section of .true-spec/project/true-spec.toml.

    Only ``structured_data_enforcement`` is read: it gates the whole PG
    plugin on or off. Safe defaults apply (enforcement on) when the TOML is
    absent or malformed.
    """

    model_config = ConfigDict(frozen=True)

    structured_data_enforcement: bool = True


class RulesConfig(BaseModel):
    """Typed configuration for the [rules] section of .true-spec/project/true-spec.toml.

    The legacy ``structured_data`` mode switch and ``allow_raw_collections``
    override are removed: the PG linter is boundary-aware and no longer gated
    by a pydantic-vs-dataclass mode. The class is retained so future advisory
    rule settings have a home.
    """

    model_config = ConfigDict(frozen=True)
