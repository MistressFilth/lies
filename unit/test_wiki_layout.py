from __future__ import annotations

from pathlib import Path

import pytest

from lies.wiki.layout import WikiLayout, _gitignore_lines, git_init_initial


@pytest.fixture(autouse=True)
def _stub_qmd_registration(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """Stub the qmd registration path so ``WikiLayout.init`` does not
    shell out to qmd AND does not transitively import the heavy
    ``pydantic_ai`` / ``fastmcp`` chain (the lazy ``__getattr__`` in
    ``lies.wiki.layout`` does ``from lies.qmd.cli import ...`` which
    pulls in ~300ms of imports even when the call itself is short-
    circuited).

    Skipped for tests whose contract is ``git_init_initial``'s own
    ``.gitignore`` writes — those tests assert on git output, not qmd.
    """
    if "git_init_initial" in request.node.name:
        return
    # Stub the lazy ``__getattr__`` itself so the heavy
    # ``from lies.qmd.cli import ...`` chain never runs. Also clear any
    # cached binding in module globals (a previous test may have
    # materialised the real qmd helper into the module namespace; the
    # ``globals().get(...) or __getattr__(...)`` pattern in
    # ``WikiLayout.init`` would otherwise pick the cached real one up
    # and shell out to qmd).
    import lies.wiki.layout as layout_mod

    monkeypatch.delattr(layout_mod, "qmd_collection_add_or_update", raising=False)
    monkeypatch.setattr(
        layout_mod,
        "__getattr__",
        lambda _name: (lambda *_a, **_kw: None) if _name == "qmd_collection_add_or_update" else (_ for _ in ()).throw(
            AttributeError(f"module 'lies.wiki.layout' has no attribute {_name!r}")
        ),
    )


@pytest.mark.slow
def test_git_init_initial_writes_gitignore_excluding_lies(tmp_path: Path) -> None:
    """``git_init_initial`` writes ``<wiki>/.gitignore`` covering ``.lies/``.

    The sidecar at ``<wiki>/.lies/memory_plans.jsonl`` is untracked on every
    fresh wiki. ``WikiMemoryService.apply_plan`` snapshots the working tree
    via ``git stash push --include-untracked``; without this ignore the
    untracked sidecar is stashed and dropped on success, silently losing
    prior sidecar lines.
    """
    WikiLayout(tmp_path).init()
    git_init_initial(tmp_path)
    gitignore = tmp_path / ".gitignore"
    assert gitignore.is_file()
    assert ".lies/\n" in gitignore.read_text(encoding="utf-8")


@pytest.mark.slow
def test_git_init_initial_does_not_clobber_existing_gitignore(tmp_path: Path) -> None:
    """A pre-existing ``.gitignore`` is preserved (M4)."""
    WikiLayout(tmp_path).init()
    custom = "# my operator-curated ignores\n*.bak\n"
    (tmp_path / ".gitignore").write_text(custom, encoding="utf-8")
    git_init_initial(tmp_path)
    gitignore = tmp_path / ".gitignore"
    assert gitignore.read_text(encoding="utf-8") == custom


@pytest.fixture
def wiki_root(tmp_path: Path) -> Path:
    (tmp_path / "raw").mkdir()
    (tmp_path / "wiki").mkdir()
    return tmp_path


def test_layout_resolves_paths(wiki_root: Path) -> None:
    layout = WikiLayout(wiki_root)
    assert layout.root == wiki_root
    assert layout.raw_dir == wiki_root / "raw"
    assert layout.wiki_dir == wiki_root / "wiki"


def test_init_creates_raw_and_wiki_dirs(tmp_path: Path) -> None:
    WikiLayout(tmp_path).init()
    assert (tmp_path / "raw").is_dir()
    assert (tmp_path / "wiki").is_dir()


def test_init_is_idempotent(tmp_path: Path) -> None:
    WikiLayout(tmp_path).init()
    # A second call must not raise; init() uses exist_ok=True.
    WikiLayout(tmp_path).init()
    assert (tmp_path / "raw").is_dir()
    assert (tmp_path / "wiki").is_dir()


def test_gitignore_lines_includes_catalog_pattern() -> None:
    """The catalog entry is anchored under ``wiki/``; root-anchored entries are gone.

    The catalog lives at ``<wiki>/wiki/.lies/catalog.db`` (not at the
    wiki root), so a single ``wiki/.lies/catalog.db*`` pattern
    subsumes the database, WAL, and SHM siblings.
    """
    lines = _gitignore_lines()
    assert any("wiki/.lies/catalog.db*" in line for line in lines)
    assert not any(line.startswith(".lies/catalog.db") for line in lines), (
        "root-anchored entries should be removed"
    )
