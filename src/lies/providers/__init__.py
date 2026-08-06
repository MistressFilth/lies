"""Provider and model configuration for LIES."""

from __future__ import annotations

from lies.providers.agents import AGENT_ROSTER
from lies.providers.config import (
    ProvidersConfig,
    ProviderSpec,
    load_providers_config,
    parse_model_string,
)
from lies.providers.env import env_override
from lies.providers.errors import ProviderConfigError
from lies.providers.registry import _client_for
from lies.providers.resolver import resolve_model

__all__ = (
    "AGENT_ROSTER",
    "ProviderConfigError",
    "ProviderSpec",
    "ProvidersConfig",
    "_client_for",
    "env_override",
    "load_providers_config",
    "parse_model_string",
    "resolve_model",
)
