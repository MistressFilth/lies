"""Tests for LIES_<AGENT>_MODEL env var precedence."""

from __future__ import annotations

import pytest

from lies.providers.env import env_override


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "LIES_ORCHESTRATOR_MODEL",
        "LIES_LINTER_MODEL",
        "LIES_SOURCE_READER_MODEL",
        "LIES_REPAIR_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)


def test_no_env_returns_none(clean_env: None) -> None:
    assert env_override("orchestrator") is None


def test_empty_env_returns_none(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIES_ORCHESTRATOR_MODEL", "")
    assert env_override("orchestrator") is None


def test_set_env_returns_value(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIES_LINTER_MODEL", "minimax:MiniMax-M3")
    assert env_override("linter") == "minimax:MiniMax-M3"


def test_uppercase_agent_name(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    # The env var name uses the agent name uppercased; underscores stay.
    monkeypatch.setenv("LIES_SOURCE_READER_MODEL", "anthropic:claude-sonnet-4-6")
    assert env_override("source_reader") == "anthropic:claude-sonnet-4-6"
