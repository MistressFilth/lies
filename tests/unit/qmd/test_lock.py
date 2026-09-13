"""Tests for src/lies/qmd/lock.py — path constants and decorator signature."""

from __future__ import annotations

import importlib
import inspect
import os


def _reload_lock_module() -> object:
    """Re-import ``lies.qmd.lock`` so module-level constants re-resolve.

    Path constants are evaluated once at import time from
    ``$LIES_QMD_LOCK_PATH`` / ``$XDG_STATE_HOME``. The conftest's
    ``_isolated_xdg`` autouse fixture mutates ``XDG_STATE_HOME`` per
    test, and individual tests may setenv ``LIES_QMD_LOCK_PATH``. A plain
    ``import_module`` returns the cached module on subsequent calls; only
    ``reload()`` re-executes the module-level statements and re-reads the
    env vars.
    """
    return importlib.reload(importlib.import_module("lies.qmd.lock"))


def test_lock_module_imports():
    mod = importlib.import_module("lies.qmd.lock")
    assert mod is not None


def test_lock_path_default_resolves_to_xdg_state_home(monkeypatch):
    """Default path is ``${XDG_STATE_HOME:-~/.local/state}/lies/qmd.lock``.

    With neither ``LIES_QMD_LOCK_PATH`` nor ``XDG_STATE_HOME`` set, the
    resolved lock path falls back to ``~/.local/state/lies/qmd.lock``.
    """
    monkeypatch.delenv("LIES_QMD_LOCK_PATH", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    mod = _reload_lock_module()
    expected = os.path.expanduser("~/.local/state/lies/qmd.lock")
    assert str(mod._LOCK_PATH) == expected


def test_lock_path_env_override_takes_precedence(monkeypatch):
    monkeypatch.setenv("LIES_QMD_LOCK_PATH", "/tmp/override-lies-qmd.lock")
    mod = _reload_lock_module()
    assert str(mod._LOCK_PATH) == "/tmp/override-lies-qmd.lock"


def test_pid_and_state_paths_share_lock_path_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("LIES_QMD_LOCK_PATH", str(tmp_path / "qmd.lock"))
    mod = _reload_lock_module()
    assert mod._PID_PATH.parent == mod._LOCK_PATH.parent
    assert mod._STATE_PATH.parent == mod._LOCK_PATH.parent
    assert mod._PID_PATH.name.startswith(mod._LOCK_PATH.name)
    assert mod._STATE_PATH.name.startswith(mod._LOCK_PATH.name)


def test_with_qmd_lock_default_signature():
    """Decorator factory exposes default timeout_s=30.0 and max_age_s=1800.0."""
    from lies.qmd.lock import with_qmd_lock

    sig = inspect.signature(with_qmd_lock)
    assert "timeout_s" in sig.parameters
    assert "max_age_s" in sig.parameters
    assert sig.parameters["timeout_s"].default == 30.0
    assert sig.parameters["max_age_s"].default == 1800.0
