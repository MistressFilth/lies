"""Per-``ProviderSpec`` ``AsyncAnthropic`` client cache.

One ``AsyncAnthropic`` instance per spec for the life of the process. All
wikis share providers, so process-scope caching is correct and avoids
re-instantiating SDK clients on every orchestrator construction.
"""

from __future__ import annotations

import os
from functools import cache

from anthropic import AsyncAnthropic

from lies.providers.config import ProviderSpec
from lies.providers.errors import ProviderConfigError


@cache
def _client_for(spec: ProviderSpec) -> AsyncAnthropic:
    key = os.environ.get(spec.api_key_env)
    if not key:
        msg = (
            f"provider {spec.name!r}: env var {spec.api_key_env!r} is unset. "
            f"Set it before running LIES."
        )
        raise ProviderConfigError(msg)
    if spec.base_url is None:  # pragma: no cover — guarded by config validation
        msg = f"provider {spec.name!r}: base_url is required for anthropic_compatible providers"
        raise ProviderConfigError(msg)
    return AsyncAnthropic(base_url=spec.base_url, api_key=key)
