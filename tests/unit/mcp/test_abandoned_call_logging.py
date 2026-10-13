"""An abandoned tool call must announce itself in the daemon log."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

import anyio
import pytest

from lies.mcp.server import AbandonedCallMiddleware


class _StubContext:
    """Minimal stand-in for ``MiddlewareContext``."""

    def __init__(self, name: str | None = "search") -> None:
        self._name = name

    def tool_name(self) -> str | None:
        return self._name

    @property
    def message(self) -> Any:
        return None


def _context(name: str | None = "search") -> _StubContext:
    return _StubContext(name)


def _failing(exc: BaseException):
    async def call_next(_context: Any) -> Any:
        raise exc

    return call_next


async def test_cancellation_is_logged_as_an_abandoned_call(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A cancelled call is logged, naming the tool and the reason."""
    middleware = AbandonedCallMiddleware()
    with caplog.at_level(logging.WARNING, logger="lies.mcp.server"):
        with pytest.raises(asyncio.CancelledError):
            await middleware.on_call_tool(_context("search"), _failing(asyncio.CancelledError()))

    records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert records, "a cancelled tool call logged nothing; the disconnect is invisible"
    message = records[0].getMessage()
    assert "search" in message, f"the log does not name the tool: {message!r}"
    assert "cancelled" in message.lower(), f"the log does not read as a cancellation: {message!r}"


@pytest.mark.parametrize(
    "exc",
    [
        anyio.ClosedResourceError(),
        anyio.BrokenResourceError(),
        anyio.EndOfStream(),
    ],
)
async def test_closed_stream_errors_are_logged(
    caplog: pytest.LogCaptureFixture, exc: BaseException
) -> None:
    """A closed or broken stream is the same event by another name."""
    middleware = AbandonedCallMiddleware()
    with caplog.at_level(logging.WARNING, logger="lies.mcp.server"):
        with pytest.raises(type(exc)):
            await middleware.on_call_tool(_context("read"), _failing(exc))

    records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert records, f"{type(exc).__name__} was logged at nothing"
    assert "read" in records[0].getMessage()


async def test_a_clean_call_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    """The normal path stays silent."""

    async def call_next(_context: Any) -> str:
        return "ok"

    middleware = AbandonedCallMiddleware()
    with caplog.at_level(logging.WARNING, logger="lies.mcp.server"):
        assert await middleware.on_call_tool(_context("search"), call_next) == "ok"

    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


async def test_an_ordinary_tool_error_is_not_reported_as_abandoned(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A tool raising a normal exception is the tool's business."""

    async def call_next(_context: Any) -> Any:
        raise ValueError("collection not found")

    middleware = AbandonedCallMiddleware()
    with caplog.at_level(logging.WARNING, logger="lies.mcp.server"):
        with pytest.raises(ValueError):
            await middleware.on_call_tool(_context("search"), call_next)

    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


async def test_the_original_exception_propagates_unchanged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Logging must not swallow or replace the failure."""
    middleware = AbandonedCallMiddleware()
    with caplog.at_level(logging.WARNING, logger="lies.mcp.server"):
        with pytest.raises(asyncio.CancelledError):
            await middleware.on_call_tool(_context("search"), _failing(asyncio.CancelledError()))
    assert [r for r in caplog.records if r.levelno >= logging.WARNING]


async def test_a_context_with_no_tool_name_still_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A context shape carrying no name must still produce a log line."""

    class _NamelessContext:
        """No ``tool_name``, no ``message.name`` -- nothing to read."""

    middleware = AbandonedCallMiddleware()
    with caplog.at_level(logging.WARNING, logger="lies.mcp.server"):
        with pytest.raises(asyncio.CancelledError):
            await middleware.on_call_tool(_NamelessContext(), _failing(asyncio.CancelledError()))

    records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert records, "a nameless context logged nothing"
    assert "unknown tool" in records[0].getMessage()


async def test_a_tool_name_attribute_that_raises_is_survivable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A ``tool_name`` that itself raises must not become a crash."""

    class _HostileContext:
        def tool_name(self) -> str:
            raise RuntimeError("naming blew up")

    middleware = AbandonedCallMiddleware()
    with caplog.at_level(logging.WARNING, logger="lies.mcp.server"):
        with pytest.raises(asyncio.CancelledError):
            await middleware.on_call_tool(_HostileContext(), _failing(asyncio.CancelledError()))

    records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert records, "a hostile tool_name suppressed the log entirely"
    assert "RuntimeError" not in records[0].getMessage()


async def test_cancelling_the_task_logs_the_cancellation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A task cancelled mid-call logs, and names no cause it cannot see."""
    never = asyncio.Event()

    async def call_next(_context: Any) -> Any:
        await never.wait()
        raise AssertionError("unreachable: the event is never set")

    middleware = AbandonedCallMiddleware()
    task = asyncio.create_task(middleware.on_call_tool(_context("search"), call_next))
    await asyncio.sleep(0)
    task.cancel()
    with caplog.at_level(logging.WARNING, logger="lies.mcp.server"):
        with contextlib.suppress(asyncio.CancelledError):
            await task

    records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert records, "a cancelled task logged nothing"
    message = records[0].getMessage()
    assert "search" in message
    assert "cancelled" in message
    # The claim the runtime does not support must not appear.
    assert "disconnect" not in message.lower(), (
        f"the log asserts a client disconnect the runtime never reports: {message!r}"
    )
