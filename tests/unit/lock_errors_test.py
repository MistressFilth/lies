"""Tests for the lock-errors hierarchy."""

from __future__ import annotations

import pytest

from lies.lock_errors import (
    WikiFlockCorrupt,
    WikiFlockError,
    WikiFlockStale,
    WikiFlockUnrepairable,
)


def test_wiki_lock_busy_subclasses_wiki_flock_error() -> None:
    from lies.errors import WikiLockBusy

    assert issubclass(WikiLockBusy, WikiFlockError)


def test_wiki_flock_stale_is_wiki_flock_error() -> None:
    err = WikiFlockStale("reaped stale wiki flock (pid=999 no longer alive); retrying")
    assert isinstance(err, WikiFlockError)


def test_wiki_flock_unrepairable_message_mentions_pid() -> None:
    err = WikiFlockUnrepairable(
        "memory flock for wiki 'mywiki' held by live pid 12345 (started 2026-08-16T18:00:00Z); "
        "force-repair failed after retry. Run `lies flock mywiki force-repair` to inspect/retry or kill 12345 manually."
    )
    msg = str(err)
    assert "pid 12345" in msg
    assert "lies flock mywiki force-repair" in msg


def test_wiki_flock_corrupt_message_references_status_command() -> None:
    err = WikiFlockCorrupt(
        "memory flock for wiki 'mywiki' has corrupt state (unreadable memory.state.json); "
        "inspect with `lies flock mywiki status`, force-repair with `lies flock mywiki force-repair`."
    )
    assert "lies flock mywiki status" in str(err)


def test_wiki_lock_busy_still_works_with_legacy_catch() -> None:
    from lies.errors import WikiLockBusy

    err = WikiLockBusy("held")
    with pytest.raises(WikiFlockError):
        raise err


def test_wiki_flock_indeterminate_message_mentions_pid_force_repair_and_kill() -> None:
    from lies.lock_errors import WikiFlockIndeterminate

    msg = (
        "mywiki flock held by an indeterminate process "
        "(pid 12345, started 1723828800); cannot determine live state. "
        "Run `lies flock mywiki force-repair` to inspect/retry or kill 12345 manually."
    )
    err = WikiFlockIndeterminate(msg)
    from lies.lock_errors import WikiFlockError

    assert isinstance(err, WikiFlockError)
    assert "pid 12345" in str(err)
    assert "lies flock mywiki force-repair" in str(err)
    assert "kill 12345" in str(err)
