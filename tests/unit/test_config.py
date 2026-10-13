"""Tests for config module."""

from __future__ import annotations

import pytest

from lies import config


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in [
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
    """The default URL is the path qmd actually serves."""
    assert config.get_qmd_url() == "http://localhost:8181/mcp"


def test_every_qmd_url_default_is_sourced_from_the_one_config_constant() -> None:
    """No module may spell its own bare-origin qmd URL."""
    import inspect

    from lies.qmd.capability import QmdCapability
    from lies.qmd.mcp import QmdMcpClient

    cap_default = inspect.signature(QmdCapability.__init__).parameters["url"].default
    assert cap_default == config.DEFAULT_QMD_URL, (
        f"QmdCapability's url default is {cap_default!r}, not the config constant"
    )
    assert QmdMcpClient().url == config.DEFAULT_QMD_URL, (
        f"QmdMcpClient's url default is {QmdMcpClient().url!r}, not the config constant"
    )

    # And the shared constant itself must carry the path, since a
    # sourced default is only as good as its source.
    assert config.DEFAULT_QMD_URL.endswith("/mcp"), (
        "qmd serves exactly one route; a bare origin 404s and reads as a down daemon"
    )


def test_get_wiki_root_removed() -> None:
    assert not hasattr(config, "get_wiki_root")
