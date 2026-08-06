"""Tests for providers.toml loading and validation."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from lies.providers.agents import AGENT_ROSTER
from lies.providers.config import (
    ProvidersConfig,
    ProviderSpec,
    load_providers_config,
)
from lies.providers.errors import ProviderConfigError


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "providers.toml"
    p.write_text(dedent(body).lstrip())
    return p


def test_load_minimal_valid_config(tmp_path: Path) -> None:
    body = """
        default_model = "anthropic:claude-opus-4-7"

        [providers.anthropic]
        type = "anthropic"
        api_key_env = "ANTHROPIC_API_KEY"

        [agents]
    """
    # Need full roster to avoid missing-agent error; append them:
    agents_block = "\n".join(f'{n} = "anthropic:claude-opus-4-7"' for n in AGENT_ROSTER)
    path = _write(tmp_path, body + "\n" + agents_block + "\n")

    cfg: ProvidersConfig = load_providers_config(path)

    assert cfg.providers == {
        "anthropic": ProviderSpec(
            name="anthropic", type="anthropic", api_key_env="ANTHROPIC_API_KEY", base_url=None
        ),
    }
    assert cfg.default_model == "anthropic:claude-opus-4-7"
    assert cfg.agents["orchestrator"] == "anthropic:claude-opus-4-7"


def test_load_minimax_provider(tmp_path: Path) -> None:
    body = """
        default_model = "anthropic:claude-opus-4-7"

        [providers.anthropic]
        type = "anthropic"
        api_key_env = "ANTHROPIC_API_KEY"

        [providers.minimax]
        type = "anthropic_compatible"
        base_url = "https://api.minimax.io/anthropic"
        api_key_env = "MINIMAX_API_KEY"

        [agents]
    """
    agents_block = "\n".join(f'{n} = "anthropic:claude-opus-4-7"' for n in AGENT_ROSTER)
    path = _write(tmp_path, body + "\n" + agents_block + "\n")

    cfg: ProvidersConfig = load_providers_config(path)

    minimax = cfg.providers["minimax"]
    assert minimax.type == "anthropic_compatible"
    assert minimax.base_url == "https://api.minimax.io/anthropic"
    assert minimax.api_key_env == "MINIMAX_API_KEY"


def test_missing_file_returns_none(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    result = load_providers_config(tmp_path / "does-not-exist.toml")
    assert result is None


def test_malformed_toml_raises(tmp_path: Path) -> None:
    path = _write(tmp_path, "this is not = valid = toml [[[\n")
    with pytest.raises(ProviderConfigError):
        load_providers_config(path)


def test_missing_agent_in_agents_table_raises(tmp_path: Path) -> None:
    # Roster minus "repair" — config is missing one entry.
    body = """
        default_model = "anthropic:claude-opus-4-7"

        [providers.anthropic]
        type = "anthropic"
        api_key_env = "ANTHROPIC_API_KEY"

        [agents]
    """
    agents_block = "\n".join(
        f'{n} = "anthropic:claude-opus-4-7"' for n in AGENT_ROSTER if n != "repair"
    )
    path = _write(tmp_path, body + "\n" + agents_block + "\n")

    with pytest.raises(ProviderConfigError, match="repair"):
        load_providers_config(path)


def test_default_model_referencing_unknown_provider_raises(tmp_path: Path) -> None:
    body = """
        default_model = "openai:gpt-5"

        [providers.anthropic]
        type = "anthropic"
        api_key_env = "ANTHROPIC_API_KEY"

        [agents]
    """
    agents_block = "\n".join(f'{n} = "anthropic:claude-opus-4-7"' for n in AGENT_ROSTER)
    path = _write(tmp_path, body + "\n" + agents_block + "\n")

    with pytest.raises(ProviderConfigError, match="openai"):
        load_providers_config(path)


def test_agent_referencing_unknown_provider_raises(tmp_path: Path) -> None:
    body = """
        default_model = "anthropic:claude-opus-4-7"

        [providers.anthropic]
        type = "anthropic"
        api_key_env = "ANTHROPIC_API_KEY"

        [agents]
    """
    roster_pairs = []
    for n in AGENT_ROSTER:
        model = "openai:gpt-5" if n == "linter" else "anthropic:claude-opus-4-7"
        roster_pairs.append(f'{n} = "{model}"')
    agents_block = "\n".join(roster_pairs)
    path = _write(tmp_path, body + "\n" + agents_block + "\n")

    with pytest.raises(ProviderConfigError, match="linter"):
        load_providers_config(path)


def test_anthropic_compatible_without_base_url_raises(tmp_path: Path) -> None:
    body = """
        default_model = "anthropic:claude-opus-4-7"

        [providers.anthropic]
        type = "anthropic"
        api_key_env = "ANTHROPIC_API_KEY"

        [providers.minimax]
        type = "anthropic_compatible"
        api_key_env = "MINIMAX_API_KEY"

        [agents]
    """
    agents_block = "\n".join(f'{n} = "anthropic:claude-opus-4-7"' for n in AGENT_ROSTER)
    path = _write(tmp_path, body + "\n" + agents_block + "\n")

    with pytest.raises(ProviderConfigError, match="base_url"):
        load_providers_config(path)


def test_parse_model_string() -> None:
    from lies.providers.config import parse_model_string

    assert parse_model_string("anthropic:claude-opus-4-7") == ("anthropic", "claude-opus-4-7")
    assert parse_model_string("minimax:MiniMax-M3") == ("minimax", "MiniMax-M3")


def test_parse_model_string_rejects_malformed() -> None:
    from lies.providers.config import parse_model_string

    with pytest.raises(ProviderConfigError):
        parse_model_string("no-colon-here")
