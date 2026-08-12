"""Wizard orchestration + atomic writer for providers.toml.

``write_atomic`` is the single IO helper every write path funnels
through (wizard + ``ops.py`` companion subcommands). Crash-mid-write
recovery inherits from the sibling-tmp + ``os.replace`` + ``fsync``
pattern that ``Registry.save`` and ``save_collection`` already use.
"""

from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass

from lies.providers.config import ProvidersConfig, ProviderSpec
from lies.providers.errors import ProviderConfigError

log = logging.getLogger(__name__)


class BootstrapAborted(Exception):
    """Wizard cancelled by the operator (^C, blank answer, invalid input)."""


class BootstrapValidationFailed(BootstrapAborted):
    """Multiple accumulated input errors; wizard will re-prompt or abort."""


class ProvidersConfigMissing(Exception):
    """Companion subcommand invoked but providers.toml does not exist.

    Distinct from the wizard's 'file-exists' guard (which lives in
    ``cli.py`` and exits 2). Companion commands raise this to suggest
    the operator run ``lies providers init`` first.
    """


@dataclass
class PartialConfig:
    providers: dict[str, ProviderSpec]
    default_model: str | None
    agents: dict[str, str]


PromptFn = Callable[[str, str], str]


def write_atomic(target_path: os.PathLike[str], partial: PartialConfig) -> None:
    """Commit ``partial`` to ``target_path`` atomically.

    Raises ``OSError`` on filesystem failure. Never leaves a partial
    file behind because ``os.replace`` is atomic on POSIX.
    """
    from lies.providers.editor import to_toml

    cfg = ProvidersConfig(
        providers=partial.providers,
        default_model=partial.default_model or "anthropic:claude-opus-4-7",
        agents=partial.agents,
    )
    target = os.fspath(target_path)
    payload = to_toml(cfg)
    directory = os.path.dirname(target) or "."
    fd, tmp = tempfile.mkstemp(prefix=".providers.toml.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
        os.chmod(target, 0o600)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


WELL_KNOWN_KEY_ENVS = (
    "ANTHROPIC_API_KEY",
    "MINIMAX_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
)


def detect_env_keys() -> dict[str, bool]:
    """Return a mapping of well-known API key env names to presence.

    Values are never logged. Used by the wizard header to show a check
    or cross next to each known name so the operator can see at a
    glance which keys they have set.
    """
    return {name: bool(os.environ.get(name)) for name in WELL_KNOWN_KEY_ENVS}


def step_default_model(partial: PartialConfig, *, prompt: PromptFn) -> None:
    """Prompt for ``provider:model``. Validates via ``parse_model_string``
    and confirms the provider is already declared.

    Raises ``ProviderConfigError`` when the operator's input references
    an undeclared provider (consistent with ``editor.apply_mutations``).
    """
    from lies.providers.config import parse_model_string

    raw = prompt(
        "Default model (provider:model) — blank to keep",
        "anthropic:claude-opus-4-7",
    ).strip()
    if not raw:
        return
    provider_name, _ = parse_model_string(raw)
    if provider_name not in partial.providers:
        msg = (
            f"step_default_model {raw!r} references undeclared "
            f"provider {provider_name!r}; declared providers: "
            f"{sorted(partial.providers)}"
        )
        raise ProviderConfigError(msg)
    partial.default_model = raw


def step_providers(partial: PartialConfig, *, prompt: PromptFn) -> None:
    """Loop asking for ``(name, type, api_key_env[, base_url])`` until the
    operator enters a blank name."""
    print("Add provider catalog entries; blank name to stop.")
    while True:
        name = prompt("  provider name (e.g. anthropic)", "").strip()
        if not name:
            return
        if name in partial.providers:
            print(f"  ✗ {name!r} already declared.")
            continue
        type_ = prompt("  type (anthropic|anthropic_compatible)", "anthropic").strip()
        if type_ not in ("anthropic", "anthropic_compatible"):
            print(f"  ✗ type must be 'anthropic' or 'anthropic_compatible', got {type_!r}")
            continue
        api_key_env = prompt("  api_key_env name (e.g. MINIMAX_API_KEY)", "").strip()
        if not api_key_env:
            print("  ✗ api_key_env required.")
            continue
        base_url: str | None = None
        if type_ == "anthropic_compatible":
            base_url = (
                prompt(
                    "  base_url (e.g. https://api.minimax.io/anthropic)",
                    "",
                ).strip()
                or None
            )
            if base_url is None:
                print("  ✗ base_url required for anthropic_compatible.")
                continue
        partial.providers[name] = ProviderSpec(
            name=name,
            type=type_,  # type: ignore[arg-type]
            api_key_env=api_key_env,
            base_url=base_url,
        )
        print(f"  ✓ added provider {name!r}.")


def step_agents(partial: PartialConfig, *, prompt: PromptFn) -> None:
    """Offer to assign every roster agent to ``default_model``. No-op
    if the operator declines or ``default_model`` is unset."""
    from lies.providers.agents import AGENT_ROSTER

    if partial.default_model is None:
        print("  (no default_model set yet; skipping agents step)")
        return
    answer = prompt("Assign default_model to every agent now? (yes/no)", "yes").strip().lower()
    if answer not in ("y", "yes"):
        return
    for agent_name in AGENT_ROSTER:
        partial.agents[agent_name] = partial.default_model
