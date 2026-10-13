"""LIES must address the qmd daemon by the name qmd binds, not a literal."""

from __future__ import annotations

import ipaddress
import socket
import threading

import pytest

from lies import config
from lies.qmd import lifecycle
from lies.qmd.daemon import daemon_port_from_url


def _url_host(url: str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(url).hostname or ""


def test_both_naming_sites_agree_and_neither_is_a_literal() -> None:
    """The URL and the TCP probe name one host, and it is a name."""
    url_host = _url_host(config.DEFAULT_QMD_URL)
    assert url_host == lifecycle._HOST, (
        f"DEFAULT_QMD_URL names {url_host!r} but the probe dials {lifecycle._HOST!r}; "
        "they resolve to different families on an IPv6-first host"
    )
    with pytest.raises(ValueError):
        ipaddress.ip_address(lifecycle._HOST)
    assert socket.getaddrinfo(lifecycle._HOST, 8181, type=socket.SOCK_STREAM)


def _loopback_listener(host: str) -> tuple[socket.socket, int]:
    srv = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, 0))
    srv.listen(5)
    threading.Thread(target=lambda: [srv.accept() for _ in range(8)], daemon=True).start()
    return srv, srv.getsockname()[1]


def test_the_probe_reaches_a_daemon_on_the_family_the_name_resolves_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A v6-only daemon is found when the name resolves v6 first, as on CI."""
    srv, port = _loopback_listener("::1")
    real = socket.getaddrinfo
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda h, p, *a, **kw: (
            [
                (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", p)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", p)),
            ]
            if h == lifecycle._HOST
            else real(h, p, *a, **kw)
        ),
    )
    try:
        with pytest.raises(ConnectionRefusedError):
            socket.create_connection(("127.0.0.1", port), timeout=0.5)
        assert lifecycle._port_listening(port) is True
    finally:
        srv.close()


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://127.0.0.1:8181/mcp", 8181),
        ("http://localhost:8181/mcp", 8181),
        ("http://localhost:9000", 9000),
        ("127.0.0.1:8181", 8181),
        ("localhost:8181", 8181),
    ],
)
def test_port_extraction_reads_both_host_forms(url: str, expected: int) -> None:
    """The port is read the same whether the host is a name or a literal."""
    assert daemon_port_from_url(url) == expected
