"""Tests for the ``lies ingest`` CLI (Task 10).

The command is registered directly on the root ``app`` via
``lies.library.cli.register`` (no intermediate sub-app wrapper). The
canonical user-facing invocation is ``lies ingest --source <PATH>`` or
``lies ingest --batch <DIR>``.

These tests cover the CLI wiring only (no actual ingest path is
exercised; the unit tests for ``run_source_ingest`` /
``run_batch_ingest`` live in ``test_ingest.py``).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lies.cli import app
from lies.library.cli import _coerce_source
from lies.library.ingest import FetchItem

runner = CliRunner()

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text or "")


def test_ingest_help_lists_source_and_batch() -> None:
    """``lies ingest --help`` lists both --source and --batch on the root app."""
    result = runner.invoke(app, ["ingest", "--help"])
    assert result.exit_code == 0, (
        f"expected exit 0; got {result.exit_code}; stderr={result.stderr!r}"
    )
    out = _strip_ansi(result.stdout)
    assert "--source" in out
    assert "--batch" in out


def test_canonical_ingest_invocation_is_reachable() -> None:
    """The spec-mandated ``lies ingest --source <PATH>`` form reaches the command body.

    Exercises the full CLI path (root app -> ``ingest`` command -> body
    entry). With no actual library configured, the body fails inside the
    library bootstrap, NOT with a Typer "no such command" / "missing
    argument" error. The exit code being non-zero is acceptable; what we
    pin is that the command was matched and dispatched (no
    ``Usage:``-style help dump, no ``No such command``).
    """
    result = runner.invoke(app, ["ingest", "--source", "/tmp/does-not-exist"])
    combined = _strip_ansi(result.stdout) + _strip_ansi(result.stderr or "")
    assert "No such command" not in combined, (
        f"ingest was not registered as a root-level command: {combined!r}"
    )
    assert "Usage:" not in combined or "--source" in combined, (
        f"ingest help dumped instead of dispatching: {combined!r}"
    )


def test_help_text_mentions_library() -> None:
    """The ``ingest`` help body should advertise the library / wiki context."""
    result = runner.invoke(
        app,
        ["ingest", "--source", "p", "--help"],
    )
    out = _strip_ansi(result.stdout).lower()
    assert "library" in out or "wiki" in out


def test_coerce_source_preserves_url_double_slash(tmp_path: Path) -> None:
    """C1: a URL string passes through with the scheme slash intact.

    ``Path("https://...")`` collapses to ``PosixPath('https:/...')``
    (single slash after scheme). The fetcher's URL-prefix check would
    reject that. The CLI must hand the URL through verbatim.
    """
    url = "https://example.com/llms.txt"
    coerced = _coerce_source(url)
    assert coerced == url
    assert isinstance(coerced, str)


def test_coerce_source_resolves_existing_filesystem_path(tmp_path: Path) -> None:
    """C1: existing filesystem paths coerce to ``Path``."""
    f = tmp_path / "exists.md"
    f.write_text("hi\n")
    coerced = _coerce_source(str(f))
    assert isinstance(coerced, Path)
    assert coerced == f


def test_coerce_source_none_returns_none() -> None:
    """C1: ``None`` is a passthrough."""
    assert _coerce_source(None) is None


def test_ingest_single_source_url_reaches_fetcher_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C1 anti-tautology: ``--source <URL>`` reaches the fetcher with double slashes.

    A stubbed fetcher records the source value it received; we assert
    the URL was passed through verbatim (no scheme mangling). This
    pins the ``lies ingest --source https://...`` end-to-end contract.
    """
    from lies import xdg

    monkeypatch.setattr(xdg, "data_home", lambda: tmp_path / "data")
    (tmp_path / "data").mkdir()

    from lies.library.paths import Library

    Library.open.cache_clear()

    seen: dict[str, object] = {"source": None}

    class _RecordingFetcher:
        def fetch_sources(self, source):  # type: ignore[no-untyped-def]
            seen["source"] = source

            def _gen():
                yield FetchItem(
                    path=Path("page.md"),
                    url=None,
                    body="body\n" * 10,
                    source_hash="abc",
                    fetched_via="rec",
                )

            return _gen()

    # ScraperFetcher is imported lazily inside the CLI body, so patch
    # at the place the body looks it up.
    monkeypatch.setattr("lies.library.fetcher.ScraperFetcher", lambda **kw: _RecordingFetcher())

    url = "https://example.com/llms.txt"
    result = runner.invoke(
        app,
        ["ingest", "--source", url, "--collection", "claude"],
    )
    # Successful run (exit 0 OR 1 is fine; what matters is the URL made it through)
    assert result.exit_code in (0, 1), (
        f"unexpected exit code; stdout={result.stdout!r}; stderr={result.stderr!r}"
    )
    src = seen["source"]
    assert src is not None
    # The URL must reach the fetcher verbatim (no scheme-slash mangling).
    if isinstance(src, str):
        assert src == url, f"URL mangled to {src!r}; expected {url!r}"
        assert src.startswith("https://")
        assert "://" in src
    else:
        # Path-typed inputs are also valid for fetcher protocol; the
        # critical contract is that a string URL stays a string URL.
        assert src.as_posix().startswith("https:/") is False or "://" in str(src)


def test_ingest_single_source_passes_slug_and_title_kwargs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I8: ``--slug custom --title "Test"`` threads through to the mirror frontmatter.

    The mirror filename ends in ``custom.md`` and the frontmatter has
    ``title: "Test"`` (not the slug-derived default). The fetcher
    stub yields one FetchItem so the pipeline writes exactly one mirror.
    """
    from lies import xdg

    monkeypatch.setattr(xdg, "data_home", lambda: tmp_path / "data")
    (tmp_path / "data").mkdir()

    from lies.library.paths import Library

    Library.open.cache_clear()
    lib = Library.open()
    lib.git_root.mkdir(parents=True, exist_ok=True)
    (lib.git_root / ".gitkeep").write_text("")

    class _OneDocFetcher:
        def fetch_sources(self, source):  # type: ignore[no-untyped-def]
            yield FetchItem(
                path=Path("orig.md"),
                url=None,
                body="body\n" * 10,
                source_hash="abc",
                fetched_via="rec",
            )

    monkeypatch.setattr("lies.library.fetcher.ScraperFetcher", lambda **kw: _OneDocFetcher())

    result = runner.invoke(
        app,
        [
            "ingest",
            "--source",
            str(tmp_path / "ignored.md"),  # not used by stub fetcher
            "--slug",
            "custom",
            "--title",
            "Test",
            "--collection",
            "claude",
        ],
    )
    combined = _strip_ansi(result.stdout) + _strip_ansi(result.stderr or "")
    assert result.exit_code in (0, 1), (
        f"unexpected exit code; stdout={result.stdout!r}; stderr={result.stderr!r}"
    )

    mirror = lib.collections_root / "claude" / "custom.md"
    assert mirror.exists(), f"mirror file {mirror} not found; combined output: {combined!r}"
    body = mirror.read_text(encoding="utf-8")
    assert 'title: "Test"' in body, f"expected custom title in frontmatter; got:\n{body[:300]}"
