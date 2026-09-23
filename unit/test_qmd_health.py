from __future__ import annotations

import socket
import threading

from lies.qmd.health import qmd_daemon_reachable


def _serve_one_connection(handler) -> tuple[socket.socket, str]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def _accept() -> None:
        sock, _ = listener.accept()
        try:
            handler(sock)
        finally:
            sock.close()
            listener.close()

    thread = threading.Thread(target=_accept, daemon=True)
    thread.start()
    return listener, f"http://127.0.0.1:{port}"


def test_reachable_when_port_accepts_connection() -> None:
    def _handle(sock: socket.socket) -> None:
        sock.recv(1)

    _listener, url = _serve_one_connection(_handle)
    try:
        assert qmd_daemon_reachable(url) is True
    finally:
        _listener.close()


def test_unreachable_when_port_refuses_connection() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    try:
        assert qmd_daemon_reachable(f"http://127.0.0.1:{port}") is False
    finally:
        sock.close()


def test_unreachable_when_url_is_malformed() -> None:
    assert qmd_daemon_reachable("not a url") is False
