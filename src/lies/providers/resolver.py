"""Resolve an agent name to a ready-to-use pydantic-ai ``Model`` (or string).

Built-in ``anthropic:`` prefixes pass through as strings — pydantic-ai's
``infer_model`` resolves them via its hardcoded provider list. Custom
``anthropic_compatible`` providers return a constructed ``AnthropicModel``
backed by an ``AsyncAnthropic`` client; ``openai_compatible`` providers
return an ``OpenAIChatModel`` backed by an ``AsyncOpenAI`` client.
The branch on ``spec.type`` narrows the client type for the matching
provider constructor — the type checker can't narrow a union across
function calls, so we dispatch inside this resolver rather than via a
generic ``_client_for`` helper.
"""

from __future__ import annotations

from anthropic import AsyncAnthropic
from openai import AsyncOpenAI
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.openai import OpenAIProvider

from lies.providers.config import (
    ProviderSpec,
    ProvidersConfig,
    read_api_key,
    resolve_agent_to_provider,
)
from lies.providers.errors import ProviderConfigError


def _anthropic_client(spec: ProviderSpec) -> AsyncAnthropic:
    """Build an ``AsyncAnthropic`` client for ``spec``."""
    if spec.base_url is None:
        raise ProviderConfigError(
            f"provider {spec.name!r}: base_url is required for anthropic-compatible providers"
        )
    return AsyncAnthropic(base_url=spec.base_url, api_key=read_api_key(spec))


def _openai_client(spec: ProviderSpec) -> AsyncOpenAI:
    """Build an ``AsyncOpenAI`` client for ``spec``."""
    if spec.base_url is None:
        raise ProviderConfigError(
            f"provider {spec.name!r}: base_url is required for openai-compatible providers"
        )
    return AsyncOpenAI(base_url=spec.base_url, api_key=read_api_key(spec))


def resolve_model(agent_name: str, config: ProvidersConfig) -> Model | str:
    """Resolve ``agent_name`` to a ready-to-use pydantic-ai ``Model`` (or string).

    Provider-key parsing + spec lookup is delegated to
    :func:`lies.providers.config.resolve_agent_to_provider` so a unit
    test can exercise the lookup path without importing pydantic_ai.
    This function then branches on ``spec.type`` to construct the
    concrete ``Model`` instance — that branch is the only piece that
    requires pydantic_ai; tests that don't reach a branch (or stub it)
    stay below the 0.15s unit-test budget.
    """
    provider_name, model_name, spec = resolve_agent_to_provider(agent_name, config)
    if spec.type == "anthropic":
        return f"anthropic:{model_name}"
    if spec.type == "openai_compatible":
        client = _openai_client(spec)
        return OpenAIChatModel(
            model_name,
            provider=OpenAIProvider(openai_client=client),
        )
    # anthropic + anthropic_compatible both use AsyncAnthropic.
    client = _anthropic_client(spec)
    return AnthropicModel(model_name, provider=AnthropicProvider(anthropic_client=client))
