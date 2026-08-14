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
from pathlib import Path

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
    print("(At least one provider is required to write providers.toml.)")
    while True:
        name = prompt("  provider name (e.g. anthropic)", "").strip()
        if not name:
            if not partial.providers:
                print("  ✗ at least one provider is required; add one or press Ctrl-C to exit.")
                continue
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


def run_wizard(
    target_path: os.PathLike[str],
    *,
    check_connection: bool,
    write_env_file: os.PathLike[str] | None,
    non_interactive: bool,
    prompt: PromptFn,
) -> None:
    """Drive the four wizard steps; commit on confirm; surface any drift
    via re-load; optionally run connectivity check; optionally write
    the .env capture.

    ``non_interactive`` is reserved; future work, currently accepted
    and ignored so the CLI flag can wire through without a flag-shaped
    change later.
    """
    del check_connection  # wired in a follow-up; accepted for CLI parity.
    if non_interactive:
        msg = "non_interactive wizard mode requires LIES_PROVIDERS_PRESET; shipped as a follow-up"
        raise NotImplementedError(msg)

    target = os.fspath(target_path)
    if os.path.dirname(target):
        os.makedirs(os.path.dirname(target), exist_ok=True)

    print("Welcome to LIES providers setup.")
    print()
    print("Detected env vars:")
    for name, present in detect_env_keys().items():
        mark = "✓" if present else "✗"
        print(f"  {mark} {name}")
    print()

    partial = PartialConfig(providers={}, default_model=None, agents={})
    # No seed. Empty catalog; first provider declared in step_providers is canonical.

    try:
        step_providers(partial, prompt=prompt)
        step_default_model(partial, prompt=prompt)
        step_agents(partial, prompt=prompt)
    except EOFError as exc:
        raise BootstrapAborted("input closed (Ctrl-D)") from exc
    except KeyboardInterrupt as exc:
        raise BootstrapAborted("interrupted (Ctrl-C)") from exc
    except ProviderConfigError as exc:
        # Wizard input referenced an undeclared provider (or another
        # validation rule fired). Surface as the wizard's typed abort
        # so the CLI can render a friendly message and exit cleanly.
        raise BootstrapAborted(str(exc)) from exc

    confirm = (
        prompt(
            f"Write to {target}? (yes/no)",
            "yes",
        )
        .strip()
        .lower()
    )
    if confirm not in ("y", "yes"):
        raise BootstrapAborted("declined to write")

    write_atomic(Path(target), partial)

    # Re-load to surface any drift between editor's output and the loader.
    # Per spec ("TOML parse error after write" → exit 2), reload errors
    # are fatal: the wizard produced a file that ``load_providers_config``
    # cannot stand behind, so we refuse to proceed to env capture or
    # final-print and let the CLI render a typed abort.
    from lies.providers.config import load_providers_config

    try:
        reloaded = load_providers_config(Path(target))
    except ProviderConfigError as exc:
        msg = f"providers.toml reloaded with errors: {exc}"
        raise BootstrapAborted(msg) from exc
    if reloaded is None:
        msg = f"post-write reload returned None; file {target} is unreadable"
        raise BootstrapAborted(msg)

    if write_env_file is not None:
        _write_env_file(write_env_file, partial)

    print()
    print(f"Written {target}.")
    print("Run `lies config` to view resolved models.")


def _write_env_file(env_path: os.PathLike[str], partial: PartialConfig) -> None:
    """Capture ``os.environ`` values for every declared ``api_key_env``
    into a chmod 600 file. Never writes key names that are not currently
    set in the operator's env, and never writes env vars from providers
    the operator did not declare in this wizard session — that would be
    a silent data egress for unrelated shell values like
    ``OPENAI_API_KEY`` set by other tools.
    """
    path = os.fspath(env_path)
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    set_keys = {
        spec.api_key_env: os.environ[spec.api_key_env]
        for spec in partial.providers.values()
        if spec.api_key_env in os.environ
    }
    fd, tmp = tempfile.mkstemp(prefix=".lies.env.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("# Generated by LIES providers bootstrap.\n")
            for key, value in set_keys.items():
                f.write(f"{key}={value}\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
