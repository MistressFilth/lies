"""Unit tests for ``Orchestrator._materialize_source``.

Branches:
- local path: must exist; copy if outside ``raw/``, pass through if
  already there.
- ``http(s)://...``: fetch via ``WebScraper.fetch``, write under
  ``raw/<collection>/<basename>``.
- ``'-'``: read all of stdin, write to a stable basename.

All failures raise ``IngestSourceUnreachable``.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from lies.memory.models import IngestSourceUnreachable
from lies.orchestrator import Orchestrator
from tests.conftest import make_wiki, models_for_tests


@pytest.fixture
def wiki(tmp_path: Path):
    """A wiki rooted at ``tmp_path`` with ``raw/`` ready to receive sources."""
    data_root = tmp_path
    (data_root / "wiki").mkdir(parents=True, exist_ok=True)
    (data_root / "raw").mkdir(parents=True, exist_ok=True)
    return make_wiki(name="materialize", data_root=data_root)


def test_materialize_local_path_returns_raw_path(wiki) -> None:
    """A local file outside ``raw/`` is copied to ``raw/<collection>/``."""
    src = wiki.data_root.parent / "incoming.md"
    src.write_text("# Hello\n", encoding="utf-8")
    orch = Orchestrator(wiki=wiki, models=models_for_tests("test"))

    out = orch._materialize_source(str(src), collection="foo")

    assert out == wiki.raw_dir / "foo" / "incoming.md"
    assert out.read_text(encoding="utf-8") == "# Hello\n"


def test_materialize_local_path_already_inside_raw_passes_through(
    wiki,
) -> None:
    """A file already inside ``raw/<collection>/`` is returned as-is
    (no copy). Detected via identity equality.
    """
    collection_dir = wiki.raw_dir / "foo"
    collection_dir.mkdir(parents=True)
    src = collection_dir / "already.md"
    src.write_text("# Already here\n", encoding="utf-8")
    orch = Orchestrator(wiki=wiki, models=models_for_tests("test"))

    out = orch._materialize_source(str(src), collection="foo")

    assert out == src
    assert out.read_text(encoding="utf-8") == "# Already here\n"


def test_materialize_url_calls_web_scraper_fetch(wiki, monkeypatch: pytest.MonkeyPatch) -> None:
    """An http(s) URL routes through ``WebScraper.fetch`` and writes the
    response under ``raw/<collection>/<basename>``.
    """
    monkeypatch.setattr(
        "lies.scrapers.web.WebScraper.fetch",
        lambda self, source: b"# Fetched body\n",
    )
    orch = Orchestrator(wiki=wiki, models=models_for_tests("test"))

    out = orch._materialize_source("https://example.com/articles/x.md", collection="foo")

    assert out == wiki.raw_dir / "foo" / "x.md"
    assert out.read_text(encoding="utf-8") == "# Fetched body\n"


def test_materialize_url_basename_falls_back_when_url_has_no_path(
    wiki, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A URL with no useful basename (e.g. ``https://example.com/``)
    writes to ``raw/<collection>/fetched.md`` so the file always lands
    in the workspace.
    """
    monkeypatch.setattr(
        "lies.scrapers.web.WebScraper.fetch",
        lambda self, source: b"body\n",
    )
    orch = Orchestrator(wiki=wiki, models=models_for_tests("test"))

    out = orch._materialize_source("https://example.com/", collection="foo")

    assert out == wiki.raw_dir / "foo" / "fetched.md"
    assert out.read_text(encoding="utf-8") == "body\n"


def test_materialize_stdin_writes_temp_file(wiki, monkeypatch: pytest.MonkeyPatch) -> None:
    """``-`` reads all of stdin and writes it to ``raw/<collection>/stdin.md``."""
    monkeypatch.setattr("sys.stdin", io.StringIO("# From stdin\n"))
    orch = Orchestrator(wiki=wiki, models=models_for_tests("test"))

    out = orch._materialize_source("-", collection="foo")

    assert out.exists()
    assert out == wiki.raw_dir / "foo" / "stdin.md"
    assert "From stdin" in out.read_text(encoding="utf-8")


def test_materialize_missing_local_path_raises_unreachable(wiki) -> None:
    """A local path that does not exist raises ``IngestSourceUnreachable``."""
    orch = Orchestrator(wiki=wiki, models=models_for_tests("test"))
    missing = wiki.data_root.parent / "does_not_exist.md"

    with pytest.raises(IngestSourceUnreachable):
        orch._materialize_source(str(missing), collection="foo")


def test_materialize_url_fetch_failure_raises_unreachable(
    wiki, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``WebScraper.fetch`` exception surfaces as ``IngestSourceUnreachable``
    with the URL embedded in the message.
    """
    from lies.scrapers.errors import ScraperFetchFailed

    def boom(self, source):
        raise ScraperFetchFailed(f"could not fetch {source}")

    monkeypatch.setattr("lies.scrapers.web.WebScraper.fetch", boom)
    orch = Orchestrator(wiki=wiki, models=models_for_tests("test"))

    with pytest.raises(IngestSourceUnreachable) as excinfo:
        orch._materialize_source("https://example.com/articles/missing", collection="foo")
    assert "https://example.com/articles/missing" in str(excinfo.value)
