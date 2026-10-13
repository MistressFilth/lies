"""Per-``ProviderSpec`` ``AsyncAnthropic`` client constructor.

Returns a fresh ``AsyncAnthropic`` instance per call, re-reading
``os.environ`` so token rotation is picked up without restart.

The SDK import is deferred into the constructor, not raised to module
scope: ``anthropic`` costs ~0.4s to import, and this module is pulled in
by ``lies.providers.__init__``, which ``tests/conftest.models_for_tests``
and every roster lookup need. See ``resolver.py`` for the same deferral.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from lies.providers.config import ProviderSpec, read_api_key
from lies.providers.errors import ProviderConfigError

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic


def _client_for(spec: ProviderSpec) -> "AsyncAnthropic":
    from anthropic import AsyncAnthropic

    key = read_api_key(spec)
    if spec.base_url is None:  # pragma: no cover — guarded by config validation
        msg = f"provider {spec.name!r}: base_url is required for anthropic_compatible providers"
        raise ProviderConfigError(msg)
    return AsyncAnthropic(base_url=spec.base_url, api_key=key)
