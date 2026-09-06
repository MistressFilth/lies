"""Per-``ProviderSpec`` ``AsyncAnthropic`` client cache.

One ``AsyncAnthropic`` instance per spec for the life of the process. All
wikis share providers, so process-scope caching is correct and avoids
re-instantiating SDK clients on every orchestrator construction.
"""

from __future__ import annotations

from functools import cache

from anthropic import AsyncAnthropic

from lies.providers import keychain
from lies.providers.config import ProviderSpec
from lies.providers.errors import ProviderConfigError


@cache
def _client_for(spec: ProviderSpec) -> AsyncAnthropic:
    api_key = keychain.resolve_api_key(spec)
    if spec.base_url is None:  # pragma: no cover — guarded by config validation
        msg = f"provider {spec.name!r}: base_url is required for anthropic_compatible providers"
        raise ProviderConfigError(msg)
    return AsyncAnthropic(base_url=spec.base_url, api_key=api_key)
