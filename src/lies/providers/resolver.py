"""Resolve an agent name to a ready-to-use pydantic-ai ``Model`` (or string).

Built-in ``anthropic:`` prefixes pass through as strings — pydantic-ai's
``infer_model`` resolves them via its hardcoded provider list. Custom
``anthropic_compatible`` providers return a constructed ``AnthropicModel``
instance backed by an ``AsyncAnthropic`` client from ``registry._client_for``.
"""

from __future__ import annotations

from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.providers.anthropic import AnthropicProvider

from lies.providers.config import ProvidersConfig, parse_model_string
from lies.providers.env import env_override
from lies.providers.registry import _client_for


def resolve_model(agent_name: str, config: ProvidersConfig) -> Model | str:
    raw = env_override(agent_name) or config.agents[agent_name]
    provider_name, model_name = parse_model_string(raw)
    spec = config.providers[provider_name]
    if spec.type == "anthropic":
        return f"anthropic:{model_name}"
    client = _client_for(spec)
    return AnthropicModel(model_name, provider=AnthropicProvider(anthropic_client=client))
