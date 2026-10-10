"""Tests for lies.lock_errors.* public exception hierarchy."""

from __future__ import annotations

from pathlib import Path

from lies.lock_errors import WikiFlockError, WikiFlockIndeterminate, WikiLockBusy, QmdLockBusy


def test_qmd_lock_busy_subclasses_wiki_flock_error():
    assert issubclass(QmdLockBusy, WikiFlockError)


def test_qmd_lock_busy_carries_diagnostic_fields():
    exc = QmdLockBusy(holder_pid=12345, waited_s=30.0, max_s=30.0)
    assert exc.holder_pid == 12345
    assert exc.waited_s == 30.0
    assert exc.max_s == 30.0
    assert "12345" in str(exc)
    assert "30" in str(exc)


def test_qmd_lock_busy_names_the_lock_path_it_waited_on():
    """The path is the only record of *which* inode was contended.

    ``LIES_QMD_LOCK_PATH`` overrides the default, so two hosts — and two
    configurations on one host — contend on different inodes while
    raising the same class. A message carrying only the pid, the wait and
    the budget tells a reader they queued but not where to look. Spec
    §Testing item 2 requires the path.
    """
    path = Path("/var/lib/lies/qmd.lock")
    exc = QmdLockBusy(holder_pid=12345, waited_s=120.0, max_s=120.0, lock_path=path)
    assert exc.lock_path == str(path)
    assert str(path) in str(exc)
    # The budget and the holder survive alongside it — the path is added,
    # not swapped for them.
    assert "12345" in str(exc)
    assert "120" in str(exc)


def test_qmd_lock_busy_omits_the_path_clause_when_no_path_is_known():
    """A hand-built instance has no inode; the clause is omitted, not blanked.

    ``QmdLockBusy`` is constructed directly in tests and by any caller
    re-wrapping one, so ``lock_path`` has to be optional and the message
    has to stay readable without it.
    """
    exc = QmdLockBusy(holder_pid=1, waited_s=1.0, max_s=2.0)
    assert exc.lock_path is None
    assert str(exc) == "qmd flock contention: holder PID 1, waited 1.0s, max 2.0s"


def test_qmd_lock_busy_optional_fields_default_to_none():
    """The ``max_s`` default is a fallback, not a description of any site.

    An earlier version of the trailing comment here said "default matches
    the spec's 30 s wait budget", which contradicted
    ``lock_errors.QmdLockBusy``'s own docstring: the budget belongs to
    the *raiser*, and the two raisers disagree (``with_qmd_lock`` polls
    30 s, ``qmd_lock_async`` 120 s). The ``30.0`` default matches no
    production raise site — it only exists so a hand-built instance has
    something to carry.

    So what is true is narrower: the default is inert, and every real
    raise overwrites it. Asserting the inertness rather than the number
    keeps the test from re-asserting a figure that is an implementation
    detail of neither raiser.
    """
    exc = QmdLockBusy()
    assert exc.holder_pid is None
    assert exc.waited_s is None
    assert exc.lock_path is None

    # Inert for a hand-built instance...
    assert exc.max_s == 30.0
    # ...and never what a raiser actually reports. The CLI raiser polls
    # 30 s by default, so 30.0 happens to coincide there; the async
    # raiser polls 120 s, so it must differ.
    assert exc.max_s != 120.0, "the default must not be read as a site budget"
    explicitly = QmdLockBusy(holder_pid=1, waited_s=119.9, max_s=120.0)
    assert explicitly.max_s == 120.0, "an explicit budget must win over the default"


def test_qmd_lock_busy_is_distinct_from_wiki_lock_busy():
    """QmdLockBusy is not WikiLockBusy — operator recovery path differs."""
    assert QmdLockBusy is not WikiLockBusy
    # WikiFlockIndeterminate is the existing indeterminate-site error;
    # QmdLockBusy should NOT match that either since the QMD path
    # always polls-then-times-out, never raises indeterminate.
    assert not isinstance(QmdLockBusy(holder_pid=1), WikiFlockIndeterminate)
