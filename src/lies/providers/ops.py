"""Companion subcommand bodies for ``lies providers …``.

Each function targets a user-level ``providers.toml``. Companion
commands raise ``ProvidersConfigMissing`` when the file is absent;
``cli.py`` translates that into a hint suggesting
``lies providers init``. Every write goes through
``bootstrap.write_atomic`` so crash-mid-write recovery is uniform.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from lies.providers.bootstrap import ProvidersConfigMissing, write_atomic
from lies.providers.config import ProviderSpec, load_providers_config
from lies.providers.editor import ProvidersMutations, apply_mutations

log = logging.getLogger(__name__)


def _load_or_raise(target: Path):
    cfg = load_providers_config(target)
    if cfg is None:
        raise ProvidersConfigMissing(f"providers.toml not found at {target}")
    return cfg


def add_provider(target: Path, spec: ProviderSpec) -> None:
    """Append a new provider to the catalog."""
    cfg = _load_or_raise(target)
    new = apply_mutations(cfg, ProvidersMutations(add_provider=spec))
    write_atomic(target, _to_partial(new))


def set_default_model(target: Path, raw: str) -> None:
    """Replace ``default_model`` with ``raw``. Provider must exist."""
    cfg = _load_or_raise(target)
    new = apply_mutations(cfg, ProvidersMutations(set_default=raw))
    write_atomic(target, _to_partial(new))


def assign_agent(target: Path, agent: str, raw: str) -> None:
    """Set ``agents[agent] = raw``. Provider must exist."""
    cfg = _load_or_raise(target)
    new = apply_mutations(cfg, ProvidersMutations(set_agents={agent: raw}))
    write_atomic(target, _to_partial(new))


def unassign_agent(target: Path, agent: str) -> None:
    """Remove ``agent`` from the agents table."""
    cfg = _load_or_raise(target)
    new = apply_mutations(cfg, ProvidersMutations(remove_agents=(agent,)))
    write_atomic(target, _to_partial(new))


def check_connectivity(target: Path) -> list[tuple[str, str, str]]:
    """Return ``[(provider_name, status, detail)]`` per provider.

    ``status`` ∈ ``{"ok", "unkeyed", "error"}``. Never raises;
    transport failures are captured as ``("error", str(exc))``.
    """
    cfg = _load_or_raise(target)
    rows: list[tuple[str, str, str]] = []
    for name, spec in cfg.providers.items():
        key = os.environ.get(spec.api_key_env)
        if not key:
            rows.append((name, "unkeyed", f"env {spec.api_key_env!r} unset"))
            continue
        try:
            _probe(spec)
            rows.append((name, "ok", "ping ok"))
        except Exception as exc:  # noqa: BLE001
            rows.append((name, "error", str(exc)))
    return rows


def _to_partial(cfg):
    """Adapters from ``ProvidersConfig`` back to ``PartialConfig`` for
    ``write_atomic``. The wizard and the companion paths share the
    same writer so they share the same crash-mid-write behavior."""
    from lies.providers.bootstrap import PartialConfig

    return PartialConfig(
        providers=cfg.providers,
        default_model=cfg.default_model,
        agents=cfg.agents,
    )


def _probe(spec: ProviderSpec) -> None:
    """Best-effort ping; raises on transport / auth failure.

    For now only ``anthropic_compatible`` providers get probed (we
    already have an AsyncAnthropic client ready). ``anthropic`` skips
    the ping because pydantic-ai's built-in provider does its own
    resolution and we don't want to import-open a client we won't use.
    """
    if spec.type != "anthropic_compatible" or not spec.base_url:
        return
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(base_url=spec.base_url, api_key=os.environ[spec.api_key_env])
    # The lightest call Anthropic-compatible endpoints expose is a
    # 1-token completion; fall back to a no-op models.list when the
    # endpoint supports it. Bridge the coroutine into the sync probe
    # surface via asyncio.run so ``check_connectivity`` stays sync.
    asyncio.run(
        client.messages.create(
            model="_probe_",
            max_tokens=1,
            messages=[{"role": "user", "content": "ping"}],
        )
    )
