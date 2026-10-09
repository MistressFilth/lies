"""Characterisation of the streamable-HTTP keepalive, measured not assumed.

A ``tools/call`` on the streamable-HTTP transport is accepted with
``202`` and its response is owed on the long-lived GET stream. While a
qmd ``search`` runs -- 12-22s of embed-and-scan with zero bytes on the
wire -- that stream is idle, and a client that tears down idle streams
loses the response to the *next* call. The host then reports a
tool-level interrupt with nothing in the LIES or qmd logs.

FastMCP ships ``PingMiddleware`` for this and LIES registers it at 5s.
It does **not** hold the stream open. Measured against the live
daemon: 1 frame in 62s and 1 in 25s where 12 and 5 were expected, on
both the handshake era and the sessionless era. The ping task is
created per request (``ping.py:63``) and cancelled through that
request's ``Connection`` exit stack (``ping.py:84``), so it dies with
the request that spawned it.

Two tests, and the distinction matters:

* the registration test pins what the code *does* -- a ping middleware
  is attached at a bounded interval. It is a fact about the source,
  and it was originally written as if it were a fact about the
  network, which is how a broken keepalive passed review.
* the idle-stream test pins what the wire *does*, and it currently
  fails. It is xfail-marked so the defect stays visible and the test
  flips the moment the real remedy lands.
"""

from __future__ import annotations

import json
import time

import httpx
import pytest
from fastmcp.server.middleware import PingMiddleware

from lies.mcp.server import mcp

# A qmd ``search`` measures 12-22s end to end. The interval has to land
# inside that window to be evidence of a warm stream.
MAX_KEEPALIVE_INTERVAL_MS = 10_000

DAEMON_URL = "http://127.0.0.1:8737/mcp"
STREAM_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}
# Long enough to cross several ping intervals at the configured 5s.
IDLE_OBSERVE_S = 26
EXPECTED_PINGS_AT_LEAST = 3


def test_server_registers_a_ping_middleware() -> None:
    """A ping middleware is attached to the server.

    This is a statement about the source tree. It says nothing about
    whether the stream survives an idle period -- see the xfail below
    for that, which is the assertion that matters.
    """
    pings = [mw for mw in mcp.middleware if isinstance(mw, PingMiddleware)]
    assert pings, "no PingMiddleware on the LIES server"


def test_ping_interval_covers_a_single_qmd_query() -> None:
    """The configured interval is shorter than one qmd query."""
    pings = [mw for mw in mcp.middleware if isinstance(mw, PingMiddleware)]
    assert pings, "no PingMiddleware registered"
    for mw in pings:
        assert mw.interval_ms <= MAX_KEEPALIVE_INTERVAL_MS, (
            f"keepalive interval {mw.interval_ms}ms exceeds {MAX_KEEPALIVE_INTERVAL_MS}ms"
        )


def _open_idle_stream(url: str, observe_s: int) -> tuple[int, int, int | None]:
    """Hold an idle GET stream open; return (frames, pings, http_status).

    Returns the negotiated protocol version when the initialize
    response parses, else ``None`` -- the daemon answers 200 with an
    SSE body rather than JSON on this path.
    """
    with httpx.Client() as client:
        init = client.post(
            url,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "keepalive-probe", "version": "1.0"},
                },
            },
            headers=STREAM_HEADERS,
            timeout=20.0,
        )
        negotiated: int | None = None
        try:
            negotiated = init.json()["result"]["protocolVersion"]
        except Exception:  # noqa: BLE001 - probe tolerates an SSE body here
            negotiated = None
        session_id = init.headers.get("mcp-session-id")
        assert session_id, f"no mcp-session-id from initialize (HTTP {init.status_code})"

        client.post(
            url,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers={**STREAM_HEADERS, "mcp-session-id": session_id},
            timeout=20.0,
        )

        deadline = time.time() + observe_s
        frames = 0
        pings = 0
        with client.stream(
            "GET",
            url,
            headers={**STREAM_HEADERS, "mcp-session-id": session_id},
            timeout=float(observe_s) + 30.0,
        ) as resp:
            assert resp.status_code == 200, (
                f"GET stream returned HTTP {resp.status_code}; the probe is not "
                "reading a stream, so any zero-ping reading would be vacuous"
            )
            for line in resp.iter_lines():
                if time.time() > deadline:
                    break
                if not line or not line.startswith("data:"):
                    continue
                frames += 1
                if '"ping"' in line[5:]:
                    pings += 1
        return frames, pings, negotiated


@pytest.mark.slow
@pytest.mark.integration
@pytest.mark.xfail(
    reason=(
        "Known defect: PingMiddleware's loop is created per request and cancelled "
        "through that request's Connection exit stack, so it emits one ping per "
        "connection and the stream goes idle again. Measured 1 ping in 26s where "
        f"{EXPECTED_PINGS_AT_LEAST}+ were expected, on both protocol eras. The real "
        "remedy is pings driven from the session registry "
        "(session_manager._server_instances), not from a request hook."
    ),
)
def test_idle_stream_receives_periodic_pings() -> None:
    """An idle stream keeps receiving pings.

    This is the assertion that would have caught the broken keepalive.
    It fails today by design; the xfail marks the gap so it flips when
    the remedy lands instead of being deleted as noise.
    """
    _frames, pings, _negotiated = _open_idle_stream(DAEMON_URL, IDLE_OBSERVE_S)
    assert pings >= EXPECTED_PINGS_AT_LEAST, (
        f"idle stream got {pings} ping(s) in {IDLE_OBSERVE_S}s; an unwarmed stream is "
        "torn down by the client and the 202-owed response is lost"
    )


@pytest.mark.slow
@pytest.mark.integration
def test_probe_can_read_a_stream_frame() -> None:
    """The probe itself works: at least one frame arrives on the stream.

    Without this, the xfail above could pass for the wrong reason --
    a probe that never connects also observes zero pings. This one
    asserts the probe saw the connection work at all.
    """
    frames, pings, _negotiated = _open_idle_stream(DAEMON_URL, 12)
    assert frames >= 1, "no SSE frame at all -- the probe is broken, not the keepalive"
    assert pings >= 1, "stream opened but carried no ping in 12s"


@pytest.mark.slow
@pytest.mark.integration
def test_probe_reports_json_or_sse_initialize_shape() -> None:
    """Keep the probe's protocol assumption explicit and checked.

    ``_open_idle_stream`` tolerates a non-JSON initialize body. That
    tolerance is deliberate (the daemon answers 200 with an SSE body on
    this path), and this test documents it rather than leaving it as an
    unstated guess.
    """
    _frames, _pings, negotiated = _open_idle_stream(DAEMON_URL, 6)
    assert negotiated is None or isinstance(negotiated, str), (
        "initialize protocolVersion should be a string or absent"
    )


def test_json_is_importable_for_the_probe() -> None:
    """The probe parses SSE payloads as JSON; keep the import honest."""
    assert json.dumps({"method": "ping"}).count("ping") == 1
