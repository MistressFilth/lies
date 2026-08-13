"""TOML loader and dataclasses for providers.toml."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from logging import getLogger
from pathlib import Path
from typing import Literal

from lies.providers.agents import AGENT_ROSTER
from lies.providers.errors import ProviderConfigError

log = getLogger(__name__)

ProviderType = Literal["anthropic", "anthropic_compatible"]


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    type: ProviderType
    api_key_env: str
    base_url: str | None = None


@dataclass(frozen=True)
class ProvidersConfig:
    providers: dict[str, ProviderSpec]
    default_model: str
    agents: dict[str, str]


def parse_model_string(raw: str) -> tuple[str, str]:
    if ":" not in raw:
        msg = f"model string must be 'provider:model', got {raw!r}"
        raise ProviderConfigError(msg)
    provider, model = raw.split(":", 1)
    if not provider or not model:
        msg = f"model string must be 'provider:model', got {raw!r}"
        raise ProviderConfigError(msg)
    return provider, model


def load_providers_config(path: Path) -> ProvidersConfig | None:
    """Load providers.toml from ``path``. Returns None when the file is missing."""
    if not path.exists():
        log.warning(
            "providers.toml not found at %s; every agent will resolve to default_model. "
            "Copy a starter file from the project docs or run `lies config` for guidance.",
            path,
        )
        return None
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        msg = f"failed to parse {path}: {exc}"
        raise ProviderConfigError(msg) from exc

    raw_providers = data.get("providers") or {}
    if not isinstance(raw_providers, dict):
        msg = f"{path}: [providers] must be a table of provider entries"
        raise ProviderConfigError(msg)

    providers: dict[str, ProviderSpec] = {}
    for name, body in raw_providers.items():
        if not isinstance(body, dict):
            msg = f"{path}: provider {name!r} must be a table"
            raise ProviderConfigError(msg)
        provider_type = body.get("type")
        api_key_env = body.get("api_key_env")
        base_url = body.get("base_url")
        if provider_type not in ("anthropic", "anthropic_compatible"):
            msg = f"{path}: provider {name!r}: type must be 'anthropic' or 'anthropic_compatible', got {provider_type!r}"
            raise ProviderConfigError(msg)
        if not isinstance(api_key_env, str) or not api_key_env:
            msg = f"{path}: provider {name!r}: api_key_env is required"
            raise ProviderConfigError(msg)
        if provider_type == "anthropic_compatible" and not base_url:
            msg = f"{path}: provider {name!r}: base_url is required for anthropic_compatible providers"
            raise ProviderConfigError(msg)
        providers[name] = ProviderSpec(
            name=name,
            type=provider_type,  # type: ignore[arg-type]
            api_key_env=api_key_env,
            base_url=base_url,
        )

    default_model = data.get("default_model")
    if not isinstance(default_model, str) or not default_model:
        msg = f"{path}: default_model is required"
        raise ProviderConfigError(msg)

    raw_agents = data.get("agents") or {}
    if not isinstance(raw_agents, dict):
        msg = f"{path}: [agents] must be a table of agent entries"
        raise ProviderConfigError(msg)

    agents: dict[str, str] = {}
    for name, model in raw_agents.items():
        if not isinstance(model, str):
            msg = f"{path}: agents.{name} must be a string"
            raise ProviderConfigError(msg)
        agents[name] = model

    # Validation pass: every model string references a declared provider.
    for label, raw in [
        ("default_model", default_model),
        *((f"agents.{n}", m) for n, m in agents.items()),
    ]:
        try:
            provider_name, _model_name = parse_model_string(raw)
        except ProviderConfigError as exc:
            msg = f"{path}: {label}: {exc}"
            raise ProviderConfigError(msg) from exc
        if provider_name not in providers:
            msg = f"{path}: {label} references undeclared provider {provider_name!r}; declared providers: {sorted(providers)}"
            raise ProviderConfigError(msg)

    # Every roster member must appear in [agents].
    missing = [name for name in AGENT_ROSTER if name not in agents]
    if missing:
        msg = (
            f"{path}: [agents] is missing entries for: {missing}. "
            f"Add one per agent name from AGENT_ROSTER."
        )
        raise ProviderConfigError(msg)

    return ProvidersConfig(
        providers=providers,
        default_model=default_model,
        agents=agents,
    )
