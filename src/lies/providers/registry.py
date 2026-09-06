"""Per-``ProviderSpec`` ``AsyncAnthropic`` client constructor.

Returns a fresh ``AsyncAnthropic`` instance per call, re-reading
``os.environ`` so token rotation is picked up without restart.
"""

from __future__ import annotations

from anthropic import AsyncAnthropic

from lies.providers.config import ProviderSpec, read_api_key
from lies.providers.errors import ProviderConfigError


def _client_for(spec: ProviderSpec) -> AsyncAnthropic:
    key = read_api_key(spec)
    if spec.base_url is None:  # pragma: no cover — guarded by config validation
        msg = f"provider {spec.name!r}: base_url is required for anthropic_compatible providers"
        raise ProviderConfigError(msg)
    return AsyncAnthropic(base_url=spec.base_url, api_key=key)
