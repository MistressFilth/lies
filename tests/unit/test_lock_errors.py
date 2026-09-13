"""Tests for lies.lock_errors.* public exception hierarchy."""

from __future__ import annotations

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


def test_qmd_lock_busy_optional_fields_default_to_none():
    exc = QmdLockBusy()
    assert exc.holder_pid is None
    assert exc.waited_s is None
    assert exc.max_s == 30.0  # default matches the spec's 30 s wait budget


def test_qmd_lock_busy_is_distinct_from_wiki_lock_busy():
    """QmdLockBusy is not WikiLockBusy — operator recovery path differs."""
    assert QmdLockBusy is not WikiLockBusy
    # WikiFlockIndeterminate is the existing indeterminate-site error;
    # QmdLockBusy should NOT match that either since the QMD path
    # always polls-then-times-out, never raises indeterminate.
    assert not isinstance(QmdLockBusy(holder_pid=1), WikiFlockIndeterminate)
