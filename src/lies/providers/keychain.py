"""Env-var chain resolver for provider API keys.

`ProviderSpec.api_key_envs` lists env-var names in priority order.
`resolve_api_key(spec)` returns the value of the first non-empty entry,
memoizing the chosen NAME keyed by `spec.name` so token rotation
mid-process is picked up without a process restart.

Single-threaded only. The module-level memo is not guarded; concurrent
callers must serialize externally.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from lies.providers.config import ProviderConfigError

if TYPE_CHECKING:
    from lies.providers.config import ProviderSpec

__all__ = ["resolve_api_key", "chosen_env_name", "invalidate", "ProviderConfigError"]

log = logging.getLogger(__name__)

# spec.name -> chosen env-var name. Module-global; cleared via invalidate().
_chosen: dict[str, str] = {}


def resolve_api_key(spec: "ProviderSpec") -> str:
    """Return the value of the first non-empty env var in spec.api_key_envs.

    Raises ProviderConfigError with the full chain enumerated when every
    entry is unset or empty/whitespace-only.
    """
    for name in spec.api_key_envs:  # ty: ignore[unresolved-attribute]
        value = os.environ.get(name)
        if value and value.strip():
            _chosen[spec.name] = name
            log.debug("provider %s resolved via %s", spec.name, name)
            return value
    raise ProviderConfigError(
        f"provider {spec.name!r} api_key_envs {list(spec.api_key_envs)} "  # ty: ignore[unresolved-attribute]
        f"all unset or empty; set one to a non-empty value"
    )


def chosen_env_name(spec: "ProviderSpec") -> str:
    """Return the env-var name that produced the most recent successful resolve.

    Raises RuntimeError if resolve_api_key has not succeeded for this spec
    in the current process.
    """
    name = _chosen.get(spec.name)
    if name is None:
        raise RuntimeError("keychain.chosen_env_name called before resolve_api_key succeeded")
    return name


def invalidate(spec: "ProviderSpec | None" = None) -> None:
    """Clear memo entries. With spec: one entry. Without: all entries."""
    if spec is None:
        _chosen.clear()
    else:
        _chosen.pop(spec.name, None)
