"""Tests for LibraryWriter (atomic-commit envelope)."""

from pathlib import Path
import sqlite3
import pytest
import subprocess
from lies.library.writer import LibraryWriter
from lies.library.catalog import (
    LibraryCatalogPage,
    open_catalog,
    list_pages,
)
from lies.library.errors import LibraryAtomicCommitFailed, LibraryCatalogLocked
from lies.library.paths import Library


@pytest.fixture
def lib(tmp_path: Path, monkeypatch) -> Library:
    from lies import xdg

    monkeypatch.setattr(xdg, "data_home", lambda: tmp_path)
    Library.open.cache_clear()
    yield Library.open()


@pytest.fixture
def lib_with_git(lib: Library) -> Library:
    """Initialise a git repo at library.git_root with a baseline commit."""
    lib.git_root.mkdir(parents=True, exist_ok=True)
    (lib.git_root / ".lies").mkdir(parents=True, exist_ok=True)
    (lib.git_root / ".gitkeep").write_text("")
    subprocess.run(
        ["git", "init", "-b", "main", str(lib.git_root)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(lib.git_root), "config", "user.email", "test@test"], check=True
    )
    subprocess.run(["git", "-C", str(lib.git_root), "config", "user.name", "test"], check=True)
    subprocess.run(["git", "-C", str(lib.git_root), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(lib.git_root), "commit", "-m", "init"], check=True, capture_output=True
    )
    return lib


def test_writer_commit_qmd_post_commit_hook_invokes_helpers(
    lib_with_git: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``commit(qmd_collection=X)`` invokes the qmd post-commit helpers.

    Regression pin for the defect where bare-name qmd lookups
    (``qmd_collection_add_or_update`` / ``qmd_update`` / ``qmd_embed``)
    inside ``LibraryWriter.commit`` raise ``NameError`` because
    Python ``LOAD_GLOBAL`` bytecode does NOT consult module-level
    ``__getattr__`` — the helpers are installed via
    :PEP:`562`'s ``__getattr__`` for *attribute access* only.
    Each call was caught by the ``except Exception`` wrapper,
    printed the "name '...' is not defined" warning, and committed
    without registering with qmd.  Library mirrors existed on disk but
    the qmd index never saw them, so every ``lies mcp query`` against
    library content fell back to the wiki ``index.md``.

    Today ``commit(qmd_collection=...)`` resolves the bare-name lookup
    via the same ``globals().get(...) or __getattr__(...)`` workaround
    :mod:`lies.cli.page` uses for its lazy ``Orchestrator`` import.
    The stubs below replace the real qmd helpers; the test asserts the
    stubs were each invoked (not the bare-name NameError swallowed).
    """
    calls: list[tuple[str, tuple]] = []

    def _stub_add_or_update(git_root, *args, **kwargs):
        calls.append(("qmd_collection_add_or_update", (git_root, args, kwargs)))

    def _stub_update(git_root, *args, **kwargs):
        calls.append(("qmd_update", (git_root, args, kwargs)))

    def _stub_embed(git_root, collection, *args, **kwargs):
        calls.append(("qmd_embed", (git_root, collection, args, kwargs)))

    # Patch at the qmd-module level so ``__getattr__`` in
    # ``lies.library.writer`` imports our stubs on first call.
    import lies.qmd.cli as qmd_mod

    monkeypatch.setattr(qmd_mod, "qmd_collection_add_or_update", _stub_add_or_update)
    monkeypatch.setattr(qmd_mod, "qmd_update", _stub_update)
    monkeypatch.setattr(qmd_mod, "qmd_embed", _stub_embed)
    # Force the writer module to re-import on next access (so the
    # patched references resolve rather than any cached ones).
    import lies.library.writer as writer_mod

    for cached in (
        "qmd_collection_add_or_update",
        "qmd_update",
        "qmd_embed",
    ):
        monkeypatch.delattr(writer_mod, cached, raising=False)

    file = lib_with_git.collections_root / "claude" / "qmd-hook-test.md"
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text("body\n")
    rel = file.relative_to(lib_with_git.git_root)

    writer = LibraryWriter(lib_with_git)
    writer.commit([rel], message="ingest: claude +qmd-test", qmd_collection="claude")

    by_name = {n for n, _ in calls}
    assert by_name == {"qmd_collection_add_or_update", "qmd_update", "qmd_embed"}, (
        f"qmd helpers must each be invoked exactly once; got {calls!r}"
    )
    # ``qmd_embed`` carries the collection name; assert it observed the
    # right collection (the operator-supplied name, not a derivation).
    embed_calls = [(n, args) for n, args in calls if n == "qmd_embed"]
    assert embed_calls and embed_calls[0][1][1] == "claude", embed_calls


def test_writer_auto_bootstrap_init_repo_on_first_commit(lib: Library) -> None:
    """Fresh library (no ``.git``) gets bootstrapped transparently on first commit.

    Regression for defect 2 from systematic-debugging triage: a fresh
    library is a plain directory; ``LibraryWriter.commit`` called
    against it raised ``LibraryAtomicCommitFailed("git add failed:
    fatal: not a git repository")``, but the CLI swallowed the failure
    (the ``typer.Exit(code=1)`` fires only on ``result.errors > 0``,
    not on library envelope exceptions) and exited 0 with 628 mirror
    files on disk and an empty catalog.  Today the writer bootstraps a
    git repo on first ``__init__``; the commit path then runs normally.

    Mirrors the wiki-side ``git_init_initial`` precedent at
    ``src/lies/wiki/layout.py:56``.  No-change for callers against an
    already-initialized repo (``.git`` already present → bootstrap
    skipped).
    """
    # Sanity: the fixture provides a bare library, no .git yet.
    assert not (lib.git_root / ".git").exists(), (
        f"test fixture should start with no .git, found one at {lib.git_root}"
    )

    writer = LibraryWriter(lib)
    # Writer __init__ ran the bootstrap.
    assert (lib.git_root / ".git").exists(), (
        f"LibraryWriter should have bootstrapped .git at {lib.git_root}"
    )
    # Initial commit has at least the catalog seed.
    git_log = subprocess.run(
        ["git", "-C", str(lib.git_root), "log", "--oneline"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert any("init" in ln.lower() for ln in git_log.stdout.splitlines()), git_log.stdout

    target = lib.collections_root / "claude" / "x.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("body\n")
    sha = writer.commit([target], message="after-bootstrap +1")
    assert sha is not None
    assert len(sha) == 40
    # Second commit lands.
    git_log2 = subprocess.run(
        ["git", "-C", str(lib.git_root), "log", "--oneline"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert any("after-bootstrap" in ln for ln in git_log2.stdout.splitlines())


def test_writer_commit_absolute_paths_are_coerced(lib_with_git: Library) -> None:
    """I12: absolute paths under ``git_root`` round-trip to repo-relative strings."""
    target = lib_with_git.collections_root / "claude" / "x.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("body\n")
    writer = LibraryWriter(lib_with_git)
    # Pass the absolute path; the writer must coerce it.
    sha = writer.commit([target], message="abs-path +1")
    assert sha is not None
    assert len(sha) == 40


def test_writer_commit_path_outside_git_root_raises_typed(
    lib_with_git: Library, tmp_path: Path
) -> None:
    """I12: a path that escapes ``git_root`` raises ``LibraryAtomicCommitFailed``.

    Previously a bare ``Path.relative_to`` raised ``ValueError`` that
    was caught by the generic envelope and misclassified as a git
    failure. The fix surfaces a typed error with a clear diagnostic.
    """
    outside = tmp_path / "outside.md"
    outside.write_text("body\n")
    writer = LibraryWriter(lib_with_git)
    with pytest.raises(LibraryAtomicCommitFailed) as exc_info:
        writer.commit([outside], message="oops")
    msg = str(exc_info.value)
    assert "outside" in msg or str(outside) in msg


def test_writer_commit_failed_message_includes_recovery_command(
    lib_with_git: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I13: ``LibraryAtomicCommitFailed`` message includes the recovery command.

    Spec §Atomic commit envelope mandates the recovery hint in the
    raised message so the operator doesn't have to read the source.
    """
    from lies.library import writer as writer_mod

    def boom(*args, **kwargs):  # type: ignore[no-untyped-def]
        from lies.wiki.git import CommitError

        raise CommitError("git commit failed: simulated")

    monkeypatch.setattr(writer_mod, "atomic_commit", boom)
    target = lib_with_git.collections_root / "claude" / "x.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("body\n")
    writer = LibraryWriter(lib_with_git)
    with pytest.raises(LibraryAtomicCommitFailed) as exc_info:
        writer.commit([target], message="oops")
    msg = str(exc_info.value)
    assert "git -C" in msg, f"expected recovery hint in message; got: {msg}"
    assert "status" in msg, f"expected `status` in recovery hint; got: {msg}"
    assert str(lib_with_git.git_root) in msg


def test_writer_commit_records_sha(lib_with_git: Library, tmp_path: Path) -> None:
    file = lib_with_git.collections_root / "claude" / "getting-started.md"
    file.parent.mkdir(parents=True)
    file.write_text("body\n")
    rel = file.relative_to(lib_with_git.git_root)
    writer = LibraryWriter(lib_with_git)
    sha = writer.commit([rel], message="ingest: claude +1")
    assert sha is not None
    assert len(sha) == 40


def test_writer_commit_no_op_returns_none(lib_with_git: Library) -> None:
    writer = LibraryWriter(lib_with_git)
    result = writer.commit([], message="empty")
    assert result is None


def test_writer_commit_catalog_only_no_paths_skips_git_commit(
    lib_with_git: Library,
) -> None:
    """Minor 27 anti-tautology: rel_paths=[] + updates=[...] short-circuits.

    Previously ``atomic_commit(files=[])`` raised ``CommitError`` which the
    envelope misclassified as ``LibraryAtomicCommitFailed``. The fix is
    symmetric: when there are no mirror files to commit, skip the git
    commit entirely but still upsert the catalog rows (the
    ``upsert_pages`` auto-commit makes a separate git commit unnecessary).
    """
    before_sha = subprocess.run(
        ["git", "-C", str(lib_with_git.git_root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    writer = LibraryWriter(lib_with_git)
    result = writer.commit(
        [],
        message="catalog-only",
        catalog_updates=[
            LibraryCatalogPage(
                slug="claude/cat-only",
                title="Cat Only",
                type="",
                source_pkg="claude",
                section="library",
                updated="2024-01-01",
                hash="",
                derived_from="",
            ),
        ],
    )
    assert result is None, "no git commit when rel_paths is empty; should return None"

    # Catalog row landed.
    conn = open_catalog(lib_with_git)
    try:
        slugs = [p.slug for p in list_pages(conn, section="library")]
    finally:
        conn.close()
    assert slugs == ["claude/cat-only"], f"catalog row not persisted; got {slugs!r}"

    # Git HEAD did NOT advance (no commit was attempted).
    after_sha = subprocess.run(
        ["git", "-C", str(lib_with_git.git_root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert after_sha == before_sha, "catalog-only short-circuit must not touch HEAD"


def test_writer_commit_catalog_updates(lib_with_git: Library) -> None:
    file = lib_with_git.collections_root / "claude" / "x.md"
    file.parent.mkdir(parents=True)
    file.write_text("body\n")
    rel = file.relative_to(lib_with_git.git_root)
    writer = LibraryWriter(lib_with_git)
    sha = writer.commit(
        [rel],
        message="ingest: claude +1",
        catalog_updates=[
            LibraryCatalogPage(
                slug="claude/x",
                title="X",
                type="",
                source_pkg="claude",
                section="library",
                updated="2024-01-01",
                hash="",
                derived_from="",
            ),
        ],
    )
    assert sha is not None
    conn = open_catalog(lib_with_git)
    try:
        slugs = [p.slug for p in list_pages(conn, section="library")]
    finally:
        conn.close()
    assert slugs == ["claude/x"]


def _sample_catalog_update(slug: str) -> LibraryCatalogPage:
    return LibraryCatalogPage(
        slug=slug,
        title="X",
        type="",
        source_pkg="claude",
        section="library",
        updated="2024-01-01",
        hash="",
        derived_from="",
    )


def test_writer_upsert_catalog_raises_locked_when_open_locked(
    lib_with_git: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins existing behavior: lock on open_catalog → LibraryCatalogLocked."""
    import lies.library.writer as writer_mod

    original_operational_error = sqlite3.OperationalError("database is locked")

    def boom_open_catalog(_library):  # type: ignore[no-untyped-def]
        raise original_operational_error

    monkeypatch.setattr(writer_mod, "open_catalog", boom_open_catalog)
    writer = LibraryWriter(lib_with_git)

    with pytest.raises(LibraryCatalogLocked) as exc_info:
        writer._upsert_catalog([_sample_catalog_update("claude/x")])
    msg = str(exc_info.value)
    assert "busy_timeout exceeded" in msg
    assert str(lib_with_git.catalog_path) in msg
    assert "during write" not in msg
    assert exc_info.value.__cause__ is original_operational_error


def test_writer_upsert_catalog_raises_locked_when_commit_locked(
    lib_with_git: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins new behavior: lock during conn.commit() → LibraryCatalogLocked."""
    import lies.library.writer as writer_mod

    real_conn = open_catalog(lib_with_git)
    original_operational_error = sqlite3.OperationalError("database is locked")

    class _StubConn:
        def __init__(self, real: sqlite3.Connection) -> None:
            self._real = real

        def executemany(self, sql, params):  # type: ignore[no-untyped-def]
            return self._real.executemany(sql, params)

        def commit(self) -> None:
            raise original_operational_error

        def close(self) -> None:
            self._real.close()

    def stub_open_catalog(_library):  # type: ignore[no-untyped-def]
        return _StubConn(real_conn)

    monkeypatch.setattr(writer_mod, "open_catalog", stub_open_catalog)
    writer = LibraryWriter(lib_with_git)

    with pytest.raises(LibraryCatalogLocked) as exc_info:
        writer._upsert_catalog([_sample_catalog_update("claude/x")])
    msg = str(exc_info.value)
    assert "busy_timeout exceeded" in msg
    assert str(lib_with_git.catalog_path) in msg
    assert "during write" in msg
    assert exc_info.value.__cause__ is original_operational_error
    # The stub's close() ran (via finally), but real_conn was already
    # closed by the stub. Verify nothing leaked by re-opening cleanly.
    conn = open_catalog(lib_with_git)
    conn.close()
