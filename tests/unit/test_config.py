"""Tests for config module."""

from __future__ import annotations

import pytest

from lies import config


def _port_of(url: str) -> int:
    """The port in ``url``, for asserting the default and the getter agree."""
    from urllib.parse import urlsplit

    return urlsplit(url).port or 9000


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
    """The default URL is the path qmd actually serves, on the name it binds."""
    # A URL without that path reaches the daemon and comes back 404, which
    # the access seam reads as a transport failure and reports as a *down*
    # daemon: a live daemon reported as absent, with the fix ("start it")
    # being the one action that would change nothing.
    #
    # The *port* is deliberately not asserted: it is a deployment choice,
    # it has changed, and ``get_qmd_port`` derives from whatever this
    # constant says. A literal port here is how this test went stale the
    # moment the default moved.
    assert config.get_qmd_url() == config.DEFAULT_QMD_URL
    assert config.get_qmd_url().startswith("http://localhost:"), (
        "qmd binds the name `localhost`, not a literal address: an IPv6-first "
        f"/etc/hosts puts it on [::1] and refuses the IPv4 dial — got "
        f"{config.get_qmd_url()!r}"
    )
    assert config.get_qmd_url().endswith("/mcp")


def test_get_qmd_port_defaults_to_the_url_default(monkeypatch) -> None:
    """The port follows ``DEFAULT_QMD_URL`` rather than a second literal."""
    monkeypatch.delenv("LIES_QMD_URL", raising=False)
    assert config.get_qmd_port() == 9000
    assert config.get_qmd_port() == _port_of(config.DEFAULT_QMD_URL)


def test_get_qmd_port_follows_an_overridden_url(monkeypatch) -> None:
    """``LIES_QMD_URL`` moves the port with it, rather than leaving a literal behind."""
    monkeypatch.setenv("LIES_QMD_URL", "http://localhost:9100/mcp")
    assert config.get_qmd_port() == 9100


def test_get_qmd_port_falls_back_on_a_malformed_url(monkeypatch) -> None:
    """A junk value falls back to the default instead of raising."""
    monkeypatch.setenv("LIES_QMD_URL", "not a url")
    assert config.get_qmd_port() == 9000


def test_every_qmd_url_default_is_sourced_from_the_one_config_constant() -> None:
    """No module may spell its own bare-origin qmd URL.

    This URL was written out in three places — ``config.DEFAULT_QMD_URL``,
    ``QmdCapability.__init__``'s ``url`` default, and
    ``QmdMcpClient.url`` — and only the first was corrected. The two
    class defaults were unreachable at the time (the sole production
    construction site, ``orchestrator.py``, passes ``get_qmd_url()``
    explicitly, and ``QmdMcpClient`` has no production construction site
    at all), so nothing failed; a future caller that omits ``url=`` would
    have reproduced the exact 404-as-down-daemon misdiagnosis.

    The fix that closes the class rather than the three instances: both
    defaults now *source* ``DEFAULT_QMD_URL``. This test asserts the
    sourcing, so a module that re-spells the literal fails here instead of
    at runtime against a live daemon.
    """
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
