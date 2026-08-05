"""Tests for wiki resolution."""

from __future__ import annotations

import pytest

from lies.wiki.wiki import Wiki


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("LIES_WIKI_NAME", raising=False)


def test_resolve_wiki_uses_default(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from lies.mcp.resolution import resolve_wiki

    Wiki.data_root_for("default").mkdir(parents=True)
    wiki = resolve_wiki()
    assert wiki.name == "default"


def test_resolve_wiki_uses_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from lies.mcp.resolution import resolve_wiki

    monkeypatch.setenv("LIES_WIKI_NAME", "research")
    Wiki.data_root_for("research").mkdir(parents=True)
    assert resolve_wiki().name == "research"


def test_resolve_wiki_explicit_name(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from lies.mcp.resolution import resolve_wiki

    Wiki.data_root_for("explicit").mkdir(parents=True)
    assert resolve_wiki("explicit").name == "explicit"
