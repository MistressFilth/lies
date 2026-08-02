"""Tests for lies.mcp.resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from lies.mcp.resolution import (
    WikiRootError,
    _resolve_wiki_root,
    _safe_page_path,
)


def test_resolve_uses_explicit_param(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit wiki_root wins over env and cwd."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LIES_WIKI_ROOT", str(tmp_path / "env-wiki"))
    target = tmp_path / "explicit"
    target.mkdir()
    (target / "wiki").mkdir()
    layout = _resolve_wiki_root(str(target))
    assert layout.root == target.resolve()


def test_resolve_falls_back_to_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No explicit param → LIES_WIKI_ROOT env."""
    monkeypatch.chdir(tmp_path)
    env_wiki = tmp_path / "env-wiki"
    env_wiki.mkdir()
    (env_wiki / "wiki").mkdir()
    monkeypatch.setenv("LIES_WIKI_ROOT", str(env_wiki))
    layout = _resolve_wiki_root(None)
    assert layout.root == env_wiki.resolve()


def test_resolve_falls_back_to_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No explicit, no env → cwd."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "wiki").mkdir()
    layout = _resolve_wiki_root(None)
    assert layout.root == tmp_path.resolve()


def test_resolve_rejects_missing_path(tmp_path: Path) -> None:
    """A path that doesn't exist raises WikiRootError."""
    with pytest.raises(WikiRootError, match="does not exist"):
        _resolve_wiki_root(str(tmp_path / "nope"))


def test_resolve_rejects_file_not_dir(tmp_path: Path) -> None:
    """A file (not a directory) raises WikiRootError."""
    f = tmp_path / "file.md"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(WikiRootError, match="not a directory"):
        _resolve_wiki_root(str(f))


def test_resolve_require_wiki_enforces_layout(tmp_path: Path) -> None:
    """With require_wiki=True, a directory lacking wiki/ and .lies/ is rejected."""
    bare = tmp_path / "bare"
    bare.mkdir()
    with pytest.raises(WikiRootError, match="wiki layout"):
        _resolve_wiki_root(str(bare), require_wiki=True)
    # require_wiki=False accepts the bare directory.
    layout = _resolve_wiki_root(str(bare), require_wiki=False)
    assert layout.root == bare.resolve()


def test_safe_page_path_rejects_traversal(tmp_path: Path) -> None:
    """A page path with .. that escapes wiki/ is rejected."""
    wiki_root = tmp_path
    (wiki_root / "wiki").mkdir()
    with pytest.raises(WikiRootError, match="escapes"):
        _safe_page_path(wiki_root, "../../etc/passwd")


def test_safe_page_path_rejects_empty(tmp_path: Path) -> None:
    """An empty page path is rejected with a structured WikiRootError."""
    wiki_root = tmp_path
    (wiki_root / "wiki").mkdir()
    with pytest.raises(WikiRootError, match="empty"):
        _safe_page_path(wiki_root, "")


def test_safe_page_path_rejects_absolute(tmp_path: Path) -> None:
    """An absolute page path is rejected (must be relative)."""
    wiki_root = tmp_path
    (wiki_root / "wiki").mkdir()
    with pytest.raises(WikiRootError, match="absolute"):
        _safe_page_path(wiki_root, "/etc/passwd")


def test_safe_page_path_resolves_relative(tmp_path: Path) -> None:
    """A safe relative page path resolves under wiki_root/wiki/."""
    wiki_root = tmp_path
    (wiki_root / "wiki" / "entities").mkdir(parents=True)
    page = wiki_root / "wiki" / "entities" / "foo.md"
    page.write_text("# foo", encoding="utf-8")
    resolved = _safe_page_path(wiki_root, "entities/foo.md")
    assert resolved == page.resolve()
