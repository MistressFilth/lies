"""Fail the unit suite when a test dials a real address.

Client-side: wraps the socket connect primitives for the whole pytest
session, so the stack it records is the *test's*. A listener on the
other side cannot do this — the connection is made from the pytest
subprocess, so the listener only ever sees its own frames.

    PYTHONPATH=tools .venv/bin/python -m pytest tests/unit -q \
        -p qmd_unit_network_audit

Wired into ``make unit-test``. It fails the session.

It records **attempts**, not successes: a refused connect still counts.
Every hit fails unless the test is named in :data:`ACCEPTED` together
with the ports it may reach. The guard re-installs itself after every
test, because a test that deletes ``socket.socket`` removes it too.
"""

import socket
import traceback
from typing import NamedTuple

import pytest

#: Loopback ports a qmd daemon is plausibly on: the default 8181, and
#: 9000 because this host runs its daemon there.
WATCHED_PORTS = frozenset({9000, 8181})

#: The only grants: ``{test node id: {ports that test may reach}}``.
#: Populated by measurement, not by reading the tests.
ACCEPTED: dict[str, frozenset[int]] = {
    # `test_status_honors_custom_port` is the one that reaches 9000, so
    # on this host it reaches the operator's LIVE daemon. Its assertions
    # do not depend on the answer — but a *new* test on 9000 would not
    # have that excuse by default, which is the point of naming it.
    "tests/unit/qmd/test_lifecycle.py::test_status_honors_custom_port": frozenset({9000}),
    # The other two lifecycle probes, on qmd's default port.
    "tests/unit/qmd/test_lifecycle.py::test_status_returns_not_running_when_pidfile_absent_and_no_listener": frozenset(
        {8181}
    ),
    "tests/unit/qmd/test_lifecycle.py::test_status_url_uses_default_port": frozenset({8181}),
    # `test_cli_qmd.py` resolves `status_cmd` end to end, same probe.
    "tests/unit/cli/test_cli_qmd.py::test_a_command_resolves_with_nothing_cached_in_globals": frozenset(
        {8181}
    ),
}

#: Hit attributed to no test — session setup, collection or teardown.
_NO_TEST = "<no test>"


class Hit(NamedTuple):
    """One connection attempt at a watched port, and who made it."""

    test: str
    address: object
    stack: str


_hits: list[Hit] = []
_current: list[str] = []


def _port_of(address: object) -> int | None:
    try:
        return int(address[1])  # type: ignore[index]
    except TypeError, IndexError, ValueError:
        return None


def _record(address: object) -> None:
    _hits.append(
        Hit(
            test=_current[-1] if _current else _NO_TEST,
            address=address,
            stack="".join(traceback.format_stack()[-14:-1]),
        )
    )


_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex


def _guarded_connect(self, address):  # type: ignore[no-untyped-def]
    if _port_of(address) in WATCHED_PORTS:
        _record(address)
    return _real_connect(self, address)


def _guarded_connect_ex(self, address):  # type: ignore[no-untyped-def]
    # `connect_ex` is a separate C method, not an alias of `connect`, so
    # patching one does not cover the other. It is included because the
    # per-file autouse guard in `test_daemon_recycle.py` names this gap
    # as its own, and citing a tool that shares it is not coverage.
    if _port_of(address) in WATCHED_PORTS:
        _record(address)
    return _real_connect_ex(self, address)


def _install() -> None:
    """Put both guards back, whoever removed them.

    Compared by identity rather than assigned unconditionally so an
    untouched session does no attribute writes per test.
    """
    if socket.socket.connect is not _guarded_connect:
        socket.socket.connect = _guarded_connect  # type: ignore[method-assign]
    if socket.socket.connect_ex is not _guarded_connect_ex:
        socket.socket.connect_ex = _guarded_connect_ex  # type: ignore[method-assign]


def _unaccepted(hits: list[Hit]) -> list[Hit]:
    """The hits no entry in :data:`ACCEPTED` grants. Everything else."""
    out = []
    for h in hits:
        granted = ACCEPTED.get(h.test)
        if granted is not None and _port_of(h.address) in granted:
            continue
        out.append(h)
    return out


def pytest_configure(config):  # noqa: ARG001
    _install()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):  # noqa: ARG001
    """Own the whole protocol, and restore the guard after it.

    A ``pytest_runtest_call`` hook alone would leave setup and teardown
    unowned, so a connect from either would be charged to whichever test
    happened to be adjacent. The post-yield ``_install`` is the part
    that makes this a guard rather than a report — see the module
    docstring.
    """
    _current.append(item.nodeid)
    try:
        yield
    finally:
        _current.pop()
        _install()


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    """Print every hit, then fail the session on the unaccepted ones.

    ``session.exitstatus`` is what ``wrap_session`` returns, and this
    hook runs before that return, so assigning it here is what makes the
    process exit non-zero.
    """
    if _hits:
        print("\n" + "!" * 70)
        print(f"*** {len(_hits)} NETWORK CONNECTION(S) TO A WATCHED PORT ***")
        for h in _hits:
            print(f"{h.test} -> connect to {h.address}")
    else:
        print("\nNETWORK GUARD: no test connected to " + str(sorted(WATCHED_PORTS)))

    stale = sorted(set(ACCEPTED) - {h.test for h in _hits})
    if stale:
        print("NOTE: accepted entries that made no connection this run: " + ", ".join(stale))

    unaccepted = _unaccepted(_hits)
    if not unaccepted:
        print(
            f"NETWORK GUARD: all {len(_hits)} hit(s) are named in qmd_unit_network_audit.ACCEPTED"
        )
        return

    print("!" * 70)
    print(f"*** {len(unaccepted)} UNACCEPTED NETWORK HIT(S) — FAILING THE SESSION ***")
    for h in unaccepted:
        granted = ACCEPTED.get(h.test)
        why = (
            f"{h.test} may reach {sorted(granted)}"
            if granted is not None
            else f"{h.test} is not in qmd_unit_network_audit.ACCEPTED"
        )
        print(f"  {h.test} -> connect to {h.address}\n    {why}\n{h.stack}")
    print("!" * 70)
    session.exitstatus = 1
