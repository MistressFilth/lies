"""Tests for the bootstrap module: atomic writer + typed aborts."""

from __future__ import annotations

from pathlib import Path

from lies.providers.bootstrap import (
    BootstrapAborted,
    BootstrapValidationFailed,
    PartialConfig,
    ProvidersConfigMissing,
    write_atomic,
)
from lies.providers.config import ProviderSpec


def _partial() -> PartialConfig:
    return PartialConfig(
        providers={
            "anthropic": ProviderSpec(
                name="anthropic",
                type="anthropic",
                api_key_env="ANTHROPIC_API_KEY",
            ),
        },
        default_model="anthropic:claude-opus-4-7",
        agents={
            "orchestrator": "anthropic:claude-opus-4-7",
            "source_reader": "anthropic:claude-opus-4-7",
            "page_writer": "anthropic:claude-opus-4-7",
            "indexer": "anthropic:claude-opus-4-7",
            "linter": "anthropic:claude-opus-4-7",
            "query_synthesizer": "anthropic:claude-opus-4-7",
            "enricher": "anthropic:claude-opus-4-7",
            "repair": "anthropic:claude-opus-4-7",
        },
    )


def test_write_atomic_creates_file_with_perms_0600(tmp_path: Path) -> None:
    target = tmp_path / "providers.toml"
    write_atomic(target, _partial())
    assert target.exists()
    mode = target.stat().st_mode & 0o777
    assert mode == 0o600


def test_write_atomic_round_trip_load(tmp_path: Path) -> None:
    target = tmp_path / "providers.toml"
    write_atomic(target, _partial())
    from lies.providers.config import load_providers_config

    loaded = load_providers_config(target)
    assert loaded is not None
    assert loaded.default_model == "anthropic:claude-opus-4-7"


def test_write_atomic_overwrite_existing(tmp_path: Path) -> None:
    target = tmp_path / "providers.toml"
    target.write_text("# stale content that will be replaced")
    write_atomic(target, _partial())
    text = target.read_text()
    assert "stale content" not in text
    assert "[providers.anthropic]" in text


def test_exceptions_are_distinct() -> None:
    """Each typed exception is its own class; hierarchy is correct."""
    assert issubclass(BootstrapValidationFailed, BootstrapAborted)
    assert not issubclass(BootstrapAborted, ProvidersConfigMissing)
    assert not issubclass(ProvidersConfigMissing, BootstrapAborted)
