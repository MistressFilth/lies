"""XDG role-routing for the sync helper's heartbeat, telemetry, and quarantine.

Verifies the locks, telemetry logs, and quarantine docs live under the
XDG role roots (``$XDG_RUNTIME_DIR``, ``$XDG_STATE_HOME``) rather
than the legacy ``<wiki>/.lies/`` location.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from lies import xdg
from lies.etl.heartbeat import (
    Heartbeat,
    acquire_create_lock,
    clear_heartbeat,
    read_heartbeat,
    release_create_lock,
    write_heartbeat,
)
from lies.etl.quarantine import list_quarantined, quarantine
from lies.etl.sync_helper import (
    acquire_heartbeat,
    collection_names,
    release_heartbeat,
)
from lies.etl.telemetry import SyncTelemetry
from lies.wiki.wiki import Wiki


@pytest.fixture
def wiki(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Wiki:
    """A Wiki with all five XDG roots under ``tmp_path`` so tests are hermetic."""
    monkeypatch.setenv("LIES_XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("LIES_XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("LIES_XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("LIES_XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("LIES_XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    name = "test"
    wiki = Wiki(
        name=name,
        data_root=Wiki.data_root_for(name),
        config_root=xdg.config_home() / "lies" / name,
        cache_root=xdg.cache_home() / "lies" / name,
        state_root=xdg.state_home() / "lies" / name,
        runtime_root=xdg.runtime_dir_for(name),
    )
    wiki.data_root.mkdir(parents=True, exist_ok=True)
    wiki.config_root.mkdir(parents=True, exist_ok=True)
    wiki.state_root.mkdir(parents=True, exist_ok=True)
    wiki.runtime_root.mkdir(parents=True, exist_ok=True)
    return wiki


def test_heartbeat_files_live_under_runtime_root(wiki: Wiki) -> None:
    """Heartbeat / create-lock / fd sidecar live under $XDG_RUNTIME_DIR."""
    h = Heartbeat(pid=1234, started_at=time.time(), collection="cpython")
    write_heartbeat(wiki, h)
    assert wiki.sync_lock_path == wiki.runtime_root / "sync.lock"
    assert wiki.sync_create_lock_path == wiki.runtime_root / "sync.lock.create"
    assert wiki.sync_fd_path == wiki.runtime_root / "sync.lock.fd"
    assert wiki.sync_lock_path.exists()
    assert not (wiki.data_root / ".lies" / "sync.lock").exists()

    read = read_heartbeat(wiki)
    assert read is not None
    assert read.pid == 1234
    assert read.collection == "cpython"

    clear_heartbeat(wiki)
    assert read_heartbeat(wiki) is None


def test_acquire_and_release_create_lock(wiki: Wiki) -> None:
    """acquire_create_lock wins / loses as expected and is idempotent on release."""
    fd1 = acquire_create_lock(wiki)
    assert fd1 is not None and fd1 > 0
    assert wiki.sync_create_lock_path.exists()

    fd2 = acquire_create_lock(wiki)
    assert fd2 is None, "second acquire must lose while the first holds"

    release_create_lock(wiki, fd1)
    assert not wiki.sync_create_lock_path.exists()

    fd3 = acquire_create_lock(wiki)
    assert fd3 is not None
    release_create_lock(wiki, fd3)


def test_acquire_and_release_heartbeat_roundtrip(wiki: Wiki) -> None:
    """acquire_heartbeat writes the heartbeat under runtime_root and releases clean."""
    hb = acquire_heartbeat(wiki, wait=False, fail_busy=True)
    assert hb is not None
    assert wiki.sync_lock_path.exists()
    assert wiki.sync_fd_path.exists()

    release_heartbeat(wiki)
    assert read_heartbeat(wiki) is None
    assert not wiki.sync_lock_path.exists()
    assert not wiki.sync_create_lock_path.exists()
    assert not wiki.sync_fd_path.exists()


def test_sync_telemetry_log_lives_under_state_root(wiki: Wiki) -> None:
    """SyncTelemetry writes ``$XDG_STATE_HOME/lies/<wiki>/logs/<c>.log``."""
    with SyncTelemetry(wiki, "cpython") as t:
        t.record_stage("scraping", docs=5, bytes_in=1024)
        t.record_counters(docs_total=5, bytes_in=1024)
        t.record_started("2026-08-01T00:00:00Z")
        t.record_ended("2026-08-01T00:01:00Z")
    log = wiki.logs_dir / "cpython.log"
    assert log == wiki.state_root / "logs" / "cpython.log"
    assert log.exists()
    text = log.read_text(encoding="utf-8")
    assert '"stage": "scraping"' in text
    assert '"kind": "counters"' in text
    assert '"kind": "started"' in text
    assert '"kind": "ended"' in text
    # Legacy location must be empty.
    assert not (wiki.data_root / ".lies" / "logs").exists()


def test_quarantine_writes_under_state_root(wiki: Wiki) -> None:
    """quarantine() writes under $XDG_STATE_HOME/lies/<wiki>/poison/."""
    raw = wiki.data_root / "raw" / "cpython" / "docs" / "broken.md"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"# broken")
    quarantine(wiki, "cpython", "docs/broken.md", "normalize failed: bad encoding")
    poison = wiki.poison_root / "cpython" / "docs" / "broken.md"
    assert poison.exists()
    assert poison.read_bytes() == b"# broken"
    sidecar = wiki.poison_root / "cpython" / "docs" / "broken.md.reason"
    assert sidecar.read_text(encoding="utf-8") == "normalize failed: bad encoding"
    out = list_quarantined(wiki, "cpython")
    assert ("docs/broken.md", "normalize failed: bad encoding") in out
    # Legacy location must be empty.
    assert not (wiki.data_root / ".lies" / "poison").exists()


def test_collection_names_globs_under_config_root(wiki: Wiki) -> None:
    """collection_names(wiki) globs ``$XDG_CONFIG_HOME/lies/<wiki>/collections/*.yaml``."""
    wiki.collections_dir.mkdir(parents=True, exist_ok=True)
    (wiki.collections_dir / "alpha.yaml").write_text("name: alpha\n", encoding="utf-8")
    (wiki.collections_dir / "beta.yaml").write_text("name: beta\n", encoding="utf-8")
    (wiki.collections_dir / "gamma.yaml.tmp").write_text("", encoding="utf-8")
    assert collection_names(wiki, None) == ["alpha", "beta"]
    assert collection_names(wiki, "alpha") == ["alpha"]


def test_no_legacy_lies_dir_is_created(wiki: Wiki) -> None:
    """No artifact migration leaves a ``<wiki>/.lies/`` directory behind."""
    raw = wiki.data_root / "raw" / "x" / "x.md"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_bytes(b"x")
    write_heartbeat(wiki, Heartbeat(pid=os.getpid(), started_at=time.time(), collection="x"))
    with SyncTelemetry(wiki, "x"):
        pass
    quarantine(wiki, "x", "x.md", "no source")
    acquire_create_lock(wiki)
    assert not (wiki.data_root / ".lies").exists()
