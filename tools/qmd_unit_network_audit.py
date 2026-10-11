"""Fail the unit suite when a test dials a real address.

Client-side: wraps the socket connect primitives for the whole pytest
session, so the stack it records is the *test's*. A listener on the
other side cannot do this — the connection is made from the pytest
subprocess, so the listener only ever sees its own frames.

    PYTHONPATH=tools .venv/bin/python -m pytest tests/unit -q \
        -p qmd_unit_network_audit

Wired into ``make unit-test``, so it runs on every commit and every
``make check``. **It fails the session.** The first version only printed,
which is how a warning reached a report as a green build.

**It records ATTEMPTS, not successes.** A connect that is refused still
counts, which is the point: what is audited is a test *reaching* for a
real address, and whether the answer is yes or no is the host's
business, not the test's.

**The rule is inverted: every hit fails unless it is named in
:data:`ACCEPTED`.** An earlier version named the known offenders as prose
in a module docstring and then reported clean anyway, so adding a new
dialing test was free. The name, and the ports it may reach, are the
whole grant; a port the name does not list is still a failure. An entry
that made no connection this run is reported, not enforced — making a
probe hermetic is progress, not a regression, and dropping the entry is
a one-line edit.

Four accepted probes exist. All four pre-date this tool, all four reach
loopback through ``lifecycle._port_listening``, and none of them asserts
on what is listening. They are here because the honest alternative —
changing them — would alter what they test.

**The guard re-installs itself after every test, and it has to.**
``tests/unit/qmd/test_daemon_recycle.py`` restores ``socket.socket``
with an explicit ``del`` (correctly — the attribute is inherited from
``_socket.socket``, so a ``monkeypatch`` undo would leave a shadowing
class attribute behind). That ``del`` removes *this* module's guard too,
and without the reinstall everything after that file runs unguarded.
Measured, not guessed: with the dial probe after that file the audit
stayed silent, and with it before, the audit failed the session. An
order-dependent guard is not a guard.

**What it still does not cover**, so nobody reads a green run as a
firewall:

- a connect performed on a thread that outlives the fixture may be
  attributed to the *next* test;
- a hit with no test in scope — session setup, collection, teardown — is
  attributed to ``<no test>`` and fails, because nothing has claimed it.

Adding a name to :data:`ACCEPTED` is the only way to acknowledge a dial,
which is what makes the list worth reading.
"""

import socket
import traceback
from typing import NamedTuple

import pytest

#: Loopback ports a qmd daemon is plausibly on. The default daemon port
#: is 8181; 9000 is here because this host runs its daemon there, which
#: is how a test that hardcoded it was caught.
WATCHED_PORTS = frozenset({9000, 8181})

#: The only grants: ``{test node id: {ports that test may reach}}``.
#: Populated by measurement, not by reading the tests.
ACCEPTED: dict[str, frozenset[int]] = {
    # `test_status_honors_custom_port` is the one that reaches 9000, so
    # on this host it reaches the operator's LIVE daemon. Its assertions
    # are on `s.port` / `s.url`, which do not depend on the answer — but
    # a *new* test on 9000 would not have that excuse by default, which
    # is the point of naming it.
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
