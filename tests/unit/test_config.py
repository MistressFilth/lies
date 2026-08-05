"""Tests for config module."""

from __future__ import annotations

import pytest

from lies import config


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in [
        "LIES_MODEL",
        "LIES_WIKI_NAME",
        "LIES_WIKI_ROOT",
        "LIES_QMD_TRANSPORT",
        "LIES_QMD_URL",
        "LIES_LOG_LEVEL",
        "LIES_XDG_DATA_HOME",
        "LIES_XDG_CONFIG_HOME",
        "LIES_XDG_CACHE_HOME",
        "LIES_XDG_STATE_HOME",
        "LIES_XDG_RUNTIME_DIR",
        "XDG_DATA_HOME",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "XDG_RUNTIME_DIR",
    ]:
        monkeypatch.delenv(k, raising=False)


def test_get_model_default() -> None:
    assert config.get_model() == "anthropic:claude-opus-4-7"


def test_get_model_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIES_MODEL", "anthropic:claude-sonnet-4-5")
    assert config.get_model() == "anthropic:claude-sonnet-4-5"


def test_get_wiki_name_default() -> None:
    assert config.get_wiki_name() == "default"


def test_get_wiki_name_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIES_WIKI_NAME", "research")
    assert config.get_wiki_name() == "research"


def test_get_xdg_data_home_uses_xdg_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", "/xdg/data")
    assert config.get_xdg_data_home() == pytest.importorskip("pathlib").Path("/xdg/data")


def test_get_qmd_transport_default() -> None:
    assert config.get_qmd_transport() == "http"


def test_get_qmd_url_default() -> None:
    assert config.get_qmd_url() == "http://127.0.0.1:8181"


def test_get_wiki_root_removed() -> None:
    assert not hasattr(config, "get_wiki_root")
