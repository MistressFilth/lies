"""Audit whether any unit test opens a TCP connection to a real address.

Client-side: wraps ``socket.socket.connect`` for the whole pytest
session, so the stack it records is the *test's*. A listener on the
other side cannot do this — the connection is made from the pytest
subprocess, so the listener only ever sees its own frames.

    PYTHONPATH=tools .venv/bin/python -m pytest tests/unit -q \
        -p qmd_unit_network_audit

**It records ATTEMPTS, not successes.** A connect that is refused still
counts, which is the point: what is being audited is a test *reaching*
for a real address, and whether the answer is yes or no is the host's
business, not the test's.

Known and accepted: ``tests/unit/qmd/test_lifecycle.py`` and
``tests/unit/cli/test_cli_qmd.py`` do reach loopback, through
``lifecycle._port_listening`` — which is the behaviour under test, it
predates this tool, and none of their assertions depend on what is
listening. Read the hits as a list of known exceptions, not as a clean
run or a dirty one.
"""

import socket
import traceback

#: Loopback ports a qmd daemon is plausibly on. The default daemon port
#: is 8181; 9000 is here because this host runs its daemon there, which
#: is how a test that hardcoded it was caught.
WATCHED_PORTS = {9000, 8181}
_hits: list[str] = []


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    if _hits:
        print("\n" + "!" * 70)
        print(f"*** {len(_hits)} NETWORK CONNECTION(S) TO A WATCHED PORT ***")
        for h in _hits:
            print(h)
        print("!" * 70)
    else:
        print("\nNETWORK GUARD: no test connected to " + str(sorted(WATCHED_PORTS)))


def pytest_configure(config):  # noqa: ARG001
    real_connect = socket.socket.connect

    def guarded(self, address):  # type: ignore[no-untyped-def]
        try:
            port = int(address[1])
        except TypeError, IndexError, ValueError:
            port = None
        if port in WATCHED_PORTS:
            _hits.append(f"connect to {address}\n" + "".join(traceback.format_stack()[-14:-1]))
        return real_connect(self, address)

    socket.socket.connect = guarded  # type: ignore[method-assign]
