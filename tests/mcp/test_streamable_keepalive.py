"""The streamable-http daemon must keep idle SSE streams warm.

A ``tools/call`` on the streamable-HTTP transport is accepted with
``202`` and its response is owed on the long-lived GET stream. While a
tool runs, that stream carries nothing: a qmd ``search`` is 12-22s of
embed-and-scan with zero bytes on the wire. A client (or an
intermediary) that tears down an idle stream therefore loses the
response to the *next* call, and the caller sees a transport-level
interrupt with no LIES-side error and no qmd log line -- the request
never reached the point of dispatching a query.

Measured on this host: a freshly started daemon serves the same
``search`` that aborts on an 8516s-idle one, so the predictor is
stream idleness, not call duration.

FastMCP ships ``PingMiddleware`` for this. The interval is bounded
here because the default (30s) is longer than the 12-22s a single
qmd query takes, so the default leaves the very window that fails
uncovered.
"""

from __future__ import annotations

from fastmcp.server.middleware import PingMiddleware

from lies.mcp.server import mcp

# A qmd ``search`` measures 12-22s end to end; three in a fan-out run
# past 60s. The keepalive must land inside that window to be evidence
# of a warm stream, so the bound is set well under the single-query
# floor rather than at it.
MAX_KEEPALIVE_INTERVAL_MS = 10_000


def test_server_registers_a_ping_middleware() -> None:
    """A keepalive middleware is registered on the server."""
    pings = [mw for mw in mcp.middleware if isinstance(mw, PingMiddleware)]
    assert pings, (
        "no PingMiddleware on the LIES server: an idle streamable-HTTP GET "
        "stream is torn down by the client, and the 202-owed response is lost"
    )


def test_ping_interval_covers_a_single_qmd_query() -> None:
    """The keepalive interval is shorter than one qmd query."""
    pings = [mw for mw in mcp.middleware if isinstance(mw, PingMiddleware)]
    assert pings, "no PingMiddleware registered"
    for mw in pings:
        assert mw.interval_ms <= MAX_KEEPALIVE_INTERVAL_MS, (
            f"keepalive interval {mw.interval_ms}ms exceeds "
            f"{MAX_KEEPALIVE_INTERVAL_MS}ms; a 12-22s qmd query would "
            f"finish inside one interval, so the stream goes idle again "
            f"before the response is delivered"
        )
