"""The network audit's own logic, tested without dialing anything.

The audit is now wired into ``make unit-test`` and exits non-zero on an
unaccepted hit, which makes it a gate rather than a report. A gate with
no test of its own is the pattern this branch has produced three times,
so its decision function is pinned directly here.

**Nothing in this file opens a socket.** Every case constructs a
:class:`~qmd_unit_network_audit.Hit` by hand. The end-to-end proof that
the wired-in plugin fails a real dial is a separate, manual step —
introduce a test that dials, run ``make unit-test``, watch it exit
non-zero — because committing a dialing test would be the very defect
the audit exists to catch.

The audit's other gap is asserted as a gap rather than left implicit:
it re-installs itself after every test because a sibling fixture's
``del socket.socket.connect`` would otherwise remove the guard for the
rest of the session. That is the ``test_the_guard_reinstalls_itself``
case below.
"""

from __future__ import annotations

import importlib.util
import socket
from pathlib import Path

import pytest

_TOOLS = Path(__file__).resolve().parents[3] / "tools"
REPO_ROOT = Path(__file__).resolve().parents[3]


def _load_audit():
    """Import the plugin by path; ``tools/`` is not a package."""
    spec = importlib.util.spec_from_file_location(
        "qmd_unit_network_audit_under_test", _TOOLS / "qmd_unit_network_audit.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


audit = _load_audit()


def _hit(test: str, port: int) -> object:
    return audit.Hit(test=test, address=("127.0.0.1", port), stack="")


def test_a_test_with_no_grant_is_unaccepted() -> None:
    """The default is failure. Only a name in ``ACCEPTED`` grants a dial."""
    hits = [_hit("tests/unit/qmd/test_new_thing.py::test_probe", 8181)]
    assert audit._unaccepted(hits) == hits, (
        "a test that is not named in ACCEPTED must be unaccepted — the "
        "rule is inverted on purpose, so adding a dialing test is not free"
    )


def test_a_named_test_may_reach_only_the_ports_it_was_granted() -> None:
    """The grant is the ports, not the file.

    Half of the accepted entries are on 8181 and one is on 9000. A grant
    of 8181 must not authorise 9000, or adding a test that reaches the
    operator's live daemon would be a one-word edit to an existing line.
    """
    on_default = next(k for k, v in audit.ACCEPTED.items() if v == frozenset({8181}))
    assert audit._unaccepted([_hit(on_default, 8181)]) == []

    wrong_port = [_hit(on_default, 9000)]
    assert audit._unaccepted(wrong_port) == wrong_port, (
        f"{on_default} is granted 8181 only; reaching 9000 reaches the "
        f"operator's live daemon and is a different grant"
    )


def test_every_accepted_name_names_a_real_test() -> None:
    """A grant must name one test, and the test must exist.

    Two failure modes, both real:

    - a **file-level** grant. ``tests/unit/qmd/test_lifecycle.py`` covers
      every test in that file, so the next one somebody adds there dials
      for free. That is the round-2 shape — the reason the audit's grants
      are node ids and not paths.
    - a **typo**. An accepted name matching nothing is indistinguishable
      from an unused grant, which the session only reports as a NOTE.

    Note that making a probe hermetic does NOT fail here: an entry whose
    test no longer dials is still a real test, and the session reports it
    as an unused grant to be dropped in a one-line edit.
    """
    for nodeid, ports in audit.ACCEPTED.items():
        rel, sep, name = nodeid.partition("::")
        assert sep and name, (
            f"{nodeid!r} is a file-level grant. Grants must be node ids: a "
            f"whole-file grant lets any future test in that file dial "
            f"without an audit noticing, which is exactly how the 9000 "
            f"regression reached a unit test in round 2."
        )
        assert ports, f"{nodeid}: an empty port set grants nothing"
        target = REPO_ROOT / rel
        assert target.is_file(), f"{nodeid}: {rel} does not exist"
        source = target.read_text(encoding="utf-8")
        assert f"def {name}(" in source, (
            f"{nodeid}: no test function named {name} in {rel}. An accepted "
            f"grant nobody can trace is not a grant."
        )


def test_the_guard_covers_both_connect_primitives() -> None:
    """``connect_ex`` is a separate C method, not an alias of ``connect``.

    Patching one does not cover the other, and the per-file autouse guard
    in ``test_daemon_recycle.py`` names this as its own gap — so citing
    the audit as covering it was wrong until this was true.
    """
    import inspect

    src = inspect.getsource(audit)
    assert "_guarded_connect_ex" in src
    assert "socket.socket.connect_ex = _guarded_connect_ex" in src
    assert audit._real_connect is socket.socket.connect
    assert audit._real_connect_ex is socket.socket.connect_ex


def test_the_guard_reinstalls_itself() -> None:
    """A sibling fixture's ``del`` must not silently end the audit.

    ``tests/unit/qmd/test_daemon_recycle.py`` restores ``socket.socket``
    with an explicit ``del`` — correctly, because ``connect`` is
    inherited from ``_socket.socket`` and a monkeypatch undo would leave
    a shadowing class attribute. That ``del`` also removes the audit's
    class attribute, so without a reinstall everything after that file
    runs unguarded.

    Measured before the fix: with a dialing test ordered *after* that
    file, the audit reported one hit and passed the session; ordered
    *before* it, it failed. An order-dependent guard is not a guard.
    """
    had_connect = "connect" in vars(socket.socket)
    saved_connect = vars(socket.socket).get("connect")
    had_ex = "connect_ex" in vars(socket.socket)
    saved_ex = vars(socket.socket).get("connect_ex")
    try:
        if had_connect:
            del socket.socket.connect
        assert socket.socket.connect is not audit._guarded_connect

        audit._install()
        assert socket.socket.connect is audit._guarded_connect
        assert socket.socket.connect_ex is audit._guarded_connect_ex
    finally:
        for name, had, saved in (
            ("connect", had_connect, saved_connect),
            ("connect_ex", had_ex, saved_ex),
        ):
            if name in vars(socket.socket):
                delattr(socket.socket, name)
            if had:
                setattr(socket.socket, name, saved)


def test_the_protocol_hook_reinstalls_rather_than_being_told_to() -> None:
    """The reinstall must live on the hook, not only be reachable by hand.

    ``test_the_guard_reinstalls_itself`` calls ``audit._install()``
    directly, so it proves the *function* works and says nothing about
    whether anything *calls* it. Deleting the call from the hook's
    ``finally`` leaves that test green — measured, as M40 in the round-4
    mutation set — and the audit silently stops guarding the second half
    of the session.

    So the hook is driven here, with a guard deliberately removed first,
    exactly as the recycle fixture leaves things.
    """
    had = "connect" in vars(socket.socket)
    saved = vars(socket.socket).get("connect")
    had_ex = "connect_ex" in vars(socket.socket)
    saved_ex = vars(socket.socket).get("connect_ex")

    class _Item:
        nodeid = "tests/unit/qmd/test_daemon_recycle.py::test_probe"

    class _Session:
        exitstatus = 0

    try:
        # Remove the guard the way the sibling fixture does, then run the
        # hook. Nothing else may reinstall it.
        if had:
            del socket.socket.connect
        if had_ex:
            del socket.socket.connect_ex

        gen = audit.pytest_runtest_protocol(_Item(), None)
        next(gen)  # the hookwrapper's pre-yield half
        for _ in gen:  # the post-yield half, where the reinstall lives
            pass

        assert socket.socket.connect is audit._guarded_connect, (
            "pytest_runtest_protocol must reinstall the guard on the way "
            "out; without it a `del socket.socket.connect` anywhere in the "
            "suite ends the audit for every test that follows"
        )
        assert socket.socket.connect_ex is audit._guarded_connect_ex
        assert audit._current == [], "the hook must pop what it pushed"

        # The reinstalled guard is *live*, not merely present. Proved by
        # calling it with a stub and a neutralised real primitive, so this
        # file never opens a socket: the audit, wired into make unit-test
        # in this same round, failed its own test file when these tests
        # dialled for real.
        _no_socket_probe()

        assert len(audit._hits) == 1, (
            "the reinstalled guard did not record a dial; the fix would be "
            "cosmetic if the attribute were set but the wrapper unused"
        )
        assert audit._hits[0].test == audit._NO_TEST, (
            "a dial with no test in scope must be attributed to <no test>, "
            "not to whichever test happens to be adjacent -- that is how a "
            "background thread's dial becomes someone else's failure"
        )
        assert audit._unaccepted(audit._hits) == audit._hits, (
            "<no test> is in no ACCEPTED entry, so it must be unaccepted"
        )
    finally:
        audit._hits.clear()
        audit._current.clear()
        for name, present, value in (
            ("connect", had, saved),
            ("connect_ex", had_ex, saved_ex),
        ):
            if name in vars(socket.socket):
                delattr(socket.socket, name)
            if present:
                setattr(socket.socket, name, value)


class _Sock:
    """A socket the audit never touches. The guard only forwards."""

    connect = staticmethod(lambda self, address: 0)
    connect_ex = staticmethod(lambda self, address: 0)


def _no_socket_probe(address: tuple[str, int] = ("127.0.0.1", 9000)) -> None:
    """Drive the installed guard without opening a socket.

    ``_real_connect`` is swapped for a recorder so the guard's fall-through
    is observed rather than performed. What is under test is the audit's
    *decision* -- record, attribute, and still call through -- not the
    kernel. A refused connect still counts as an attempt, so nothing is
    lost by not making one.
    """
    saved = audit._real_connect
    audit._real_connect = lambda self, addr: addr  # type: ignore[assignment]
    try:
        audit._guarded_connect(_Sock(), address)
    finally:
        audit._real_connect = saved


@pytest.mark.parametrize(
    ("primitive", "port"),
    [("_guarded_connect", 9000), ("_guarded_connect_ex", 8181)],
)
def test_a_watched_port_is_recorded_against_the_running_test(primitive: str, port: int) -> None:
    """Each primitive records, attributes, and still falls through.

    **Nothing here opens a socket.** An earlier version called
    ``socket.socket().connect()`` for real, and the audit -- wired into
    ``make unit-test`` in this same round -- failed the session on its own
    test file. The tempting fix was to add these three tests to
    ``ACCEPTED``, which would have put the audit inside the audit's own
    exemption list.
    """
    seen: list[object] = []
    saved_c = audit._real_connect
    saved_e = audit._real_connect_ex
    audit._real_connect = lambda self, addr: seen.append(addr) or 0  # type: ignore[assignment]
    audit._real_connect_ex = lambda self, addr: seen.append(addr) or 0  # type: ignore[assignment]

    audit._hits.clear()
    audit._current.clear()
    audit._current.append("tests/unit/qmd/test_daemon_recycle.py::test_probe")
    try:
        getattr(audit, primitive)(_Sock(), ("127.0.0.1", port))
        assert len(audit._hits) == 1, f"{primitive} did not record a watched port"
        hit = audit._hits[0]
        assert audit._port_of(hit.address) == port
        assert hit.test == "tests/unit/qmd/test_daemon_recycle.py::test_probe"
        assert seen == [("127.0.0.1", port)], (
            f"{primitive} must still fall through to the real primitive; "
            f"the audit observes, it does not block"
        )
        assert audit._unaccepted(audit._hits) == audit._hits, (
            "test_probe is in no ACCEPTED entry, so its hit must be "
            "unaccepted -- these tests rely on that rule holding"
        )
    finally:
        audit._hits.clear()
        audit._current.clear()
        audit._real_connect = saved_c
        audit._real_connect_ex = saved_e


def test_an_unwatched_port_is_not_recorded() -> None:
    """A port no daemon plausibly listens on is not this audit's business.

    Otherwise the plugin fails a test that legitimately talks to a
    fixture on an ephemeral port, and the fix for that is to widen
    ``WATCHED_PORTS`` -- which is how a guard stops guarding.
    """
    seen: list[object] = []
    saved = audit._real_connect
    audit._real_connect = lambda self, addr: seen.append(addr) or 0  # type: ignore[assignment]
    audit._hits.clear()
    try:
        audit._guarded_connect(_Sock(), ("127.0.0.1", 5432))
        assert audit._hits == []
        assert seen == [("127.0.0.1", 5432)]
    finally:
        audit._hits.clear()
        audit._real_connect = saved
