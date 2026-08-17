import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import pytest

from lies.collections.record import Collection
from lies.etl.stages.normalize import run_normalize
from lies.etl.stages.scrape import run_scrape
from lies.etl.stages.write import run_write
from lies.scrapers.base import ParsedDoc
from lies.wiki.wiki import Wiki


def _wiki(tmp_path: Path) -> Wiki:
    wiki = Wiki(
        name="cpython",
        data_root=tmp_path,
        config_root=tmp_path,
        cache_root=tmp_path,
        state_root=tmp_path,
        runtime_root=tmp_path,
    )
    wiki.raw_dir.mkdir(parents=True, exist_ok=True)
    wiki.wiki_dir.mkdir(parents=True, exist_ok=True)
    wiki.scratch_dir.mkdir(parents=True, exist_ok=True)
    wiki.cache_root.mkdir(parents=True, exist_ok=True)
    return wiki


def _collection(tmp_path: Path) -> Collection:
    return Collection(
        name="cpython",
        path=tmp_path / "raw" / "cpython",
        source="https://example.com",
        tags=[],
        scraper_cmd=None,
        doc_path=None,
        mapper_model=None,
        language=None,
        version="1.0.0",
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
        updated_at=datetime(2026, 8, 1, tzinfo=UTC),
    )


def test_run_scrape_invokes_pick_scraper(tmp_path: Path) -> None:
    fake_scraper = mock.Mock()
    fake_scraper.fetch.return_value = b"# hello"
    fake_docs = [
        ParsedDoc(path="x.md", content=b"# hello", source_sha256="abc", source_format="markdown"),
    ]
    fake_scraper.parse.return_value = fake_docs
    fake_scraper.emit_manifest.return_value = tmp_path / "raw" / "cpython" / "manifest.json"
    collection = _collection(tmp_path)
    wiki = _wiki(tmp_path)
    with mock.patch("lies.etl.stages.scrape.pick_scraper", return_value=fake_scraper) as mock_pick:
        result = run_scrape(wiki, collection)
    mock_pick.assert_called_once_with(collection.source)
    fake_scraper.fetch.assert_called_once_with(collection.source)
    fake_scraper.parse.assert_called_once_with(b"# hello")
    fake_scraper.emit_manifest.assert_called_once_with(fake_docs, wiki.raw_dir / collection.name)
    assert (wiki.raw_dir / collection.name).is_dir()
    assert (wiki.cache_root / "collections" / collection.name / "manifest.json").exists()
    assert result.success == ["x.md"]
    assert result.parsed_docs == fake_docs
    assert result.bytes_in == len(b"# hello")


def test_run_normalize_dispatches_and_applies_obsidian(tmp_path: Path) -> None:
    fake_doc = ParsedDoc(
        path="x.md", content=b"# Hello", source_sha256="abc", source_format="markdown"
    )
    with (
        mock.patch("lies.etl.stages.normalize.format_dispatch.dispatch", return_value="# Hello"),
        mock.patch(
            "lies.etl.stages.normalize.obsidian.apply", side_effect=lambda m, **kw: m
        ) as obs,
    ):
        result = run_normalize(_wiki(tmp_path), _collection(tmp_path), [fake_doc])
    assert result.success == ["x.md"]
    obs.assert_called_once()
    # normalize stage emits parsed_docs carrying the markdown
    assert len(result.parsed_docs) == 1


def test_run_normalize_quarantines_unknown_format(tmp_path: Path) -> None:
    from lies.etl.normalize.format_dispatch import UnknownFormatError

    fake_doc = ParsedDoc(path="bad.xyz", content=b"x", source_sha256="abc", source_format="weird")
    with mock.patch(
        "lies.etl.stages.normalize.format_dispatch.dispatch",
        side_effect=UnknownFormatError("nope"),
    ):
        result = run_normalize(_wiki(tmp_path), _collection(tmp_path), [fake_doc])
    assert result.quarantined == [("bad.xyz", "nope")]


def test_run_write_atomic_commits_new_files(tmp_path: Path) -> None:
    fake_normalized = [("x.md", "# body")]
    manifest = mock.Mock()
    manifest.compare.return_value = False  # fresh manifest: no prior entries
    with mock.patch("lies.etl.stages.write.atomic_commit") as ac:
        result = run_write(
            _wiki(tmp_path),
            _collection(tmp_path),
            fake_normalized,
            manifest=manifest,
            force=False,
        )
    assert result.success == ["x.md"]
    ac.assert_called_once()
    written = tmp_path / "wiki" / "x.md"
    assert written.exists()


def test_run_write_writes_under_wiki_root(tmp_path: Path) -> None:
    """Target path is wiki.wiki_dir/<path>, not CWD-relative."""
    fake_normalized = [("concepts/example.md", "# body")]
    manifest = mock.Mock()
    manifest.compare.return_value = False
    with mock.patch("lies.etl.stages.write.atomic_commit"):
        run_write(
            _wiki(tmp_path),
            _collection(tmp_path),
            fake_normalized,
            manifest=manifest,
            force=False,
        )
    assert (tmp_path / "wiki" / "concepts" / "example.md").exists()


def test_run_write_skips_unchanged(tmp_path: Path) -> None:
    manifest = mock.Mock()
    manifest.compare.return_value = True
    fake_normalized = [("x.md", "# body")]
    with mock.patch("lies.etl.stages.write.atomic_commit") as ac:
        result = run_write(
            _wiki(tmp_path),
            _collection(tmp_path),
            fake_normalized,
            manifest=manifest,
            force=False,
        )
    assert result.skipped == ["x.md"]
    ac.assert_not_called()


def test_run_write_quarantines_on_oserror(tmp_path: Path) -> None:
    fake_normalized = [("x.md", "# body")]
    manifest = mock.Mock()
    manifest.compare.return_value = False
    with (
        mock.patch("lies.etl.stages.write.atomic_commit"),
        mock.patch("builtins.open", side_effect=OSError("disk full")),
    ):
        result = run_write(
            _wiki(tmp_path),
            _collection(tmp_path),
            fake_normalized,
            manifest=manifest,
            force=False,
        )
    assert result.quarantined == [("x.md", "disk full")]


def test_run_write_respects_force(tmp_path: Path) -> None:
    manifest = mock.Mock()
    manifest.compare.return_value = True
    fake_normalized = [("x.md", "# body")]
    with mock.patch("lies.etl.stages.write.atomic_commit") as ac:
        result = run_write(
            _wiki(tmp_path),
            _collection(tmp_path),
            fake_normalized,
            manifest=manifest,
            force=True,
        )
    assert result.success == ["x.md"]
    assert result.skipped == []
    ac.assert_called_once()


def test_run_write_registers_collection_with_qmd_post_commit(tmp_path: Path) -> None:
    """After WRITE commits, qmd_collection_add_if_missing must be invoked
    for the wiki's collection so the qmd derived index can find it."""
    wiki = _wiki(tmp_path)
    (wiki.raw_dir / "cpython").mkdir(parents=True, exist_ok=True)
    manifest = mock.Mock()
    manifest.compare.return_value = False
    fake_normalized = [("x.md", "# body")]
    with (
        mock.patch("lies.etl.stages.write.atomic_commit"),
        mock.patch("lies.etl.stages.write.qmd_collection_add_if_missing") as m_add,
        mock.patch("lies.etl.stages.write.qmd_update"),
        mock.patch("lies.etl.stages.write.rebuild_index"),
    ):
        run_write(
            wiki,
            _collection(tmp_path),
            fake_normalized,
            manifest=manifest,
            force=False,
        )
    m_add.assert_called_once()
    args, _ = m_add.call_args
    # First arg: cwd (raw_dir). Second arg: path to register (raw_dir/<name>).
    # Third arg: qmd collection name.
    assert args[0] == wiki.raw_dir
    assert args[1] == wiki.raw_dir / "cpython"
    assert args[2] == "cpython"


def test_run_write_refreshes_qmd_index_post_commit(tmp_path: Path) -> None:
    """After WRITE commits, qmd_update must be invoked against the wiki root
    so the derived index picks up the freshly-written files."""
    wiki = _wiki(tmp_path)
    (wiki.raw_dir / "cpython").mkdir(parents=True, exist_ok=True)
    manifest = mock.Mock()
    manifest.compare.return_value = False
    fake_normalized = [("x.md", "# body")]
    with (
        mock.patch("lies.etl.stages.write.atomic_commit"),
        mock.patch("lies.etl.stages.write.qmd_collection_add_if_missing"),
        mock.patch("lies.etl.stages.write.qmd_update") as m_update,
        mock.patch("lies.etl.stages.write.rebuild_index"),
    ):
        run_write(
            wiki,
            _collection(tmp_path),
            fake_normalized,
            manifest=manifest,
            force=False,
        )
    m_update.assert_called_once_with(wiki.data_root)


def test_run_write_regenerates_index_md_post_commit(tmp_path: Path) -> None:
    """After WRITE commits, wiki/index.md must be regenerated from the
    freshly-written pages so the qmd-fallback synthesizer can navigate."""
    wiki = _wiki(tmp_path)
    (wiki.raw_dir / "cpython").mkdir(parents=True, exist_ok=True)
    manifest = mock.Mock()
    manifest.compare.return_value = False
    fake_normalized = [("x.md", "# body")]
    with (
        mock.patch("lies.etl.stages.write.atomic_commit"),
        mock.patch("lies.etl.stages.write.qmd_collection_add_if_missing"),
        mock.patch("lies.etl.stages.write.qmd_update"),
        mock.patch("lies.etl.stages.write.rebuild_index") as m_index,
    ):
        run_write(
            wiki,
            _collection(tmp_path),
            fake_normalized,
            manifest=manifest,
            force=False,
        )
    m_index.assert_called_once_with(wiki)


def test_run_write_skips_qmd_hooks_when_nothing_committed(tmp_path: Path) -> None:
    """When the WRITE stage has no files to commit (skipped only), the
    qmd hooks must not run — the wiki state did not change."""
    manifest = mock.Mock()
    manifest.compare.return_value = True  # pretend everything unchanged
    fake_normalized = [("x.md", "# body")]
    with (
        mock.patch("lies.etl.stages.write.atomic_commit") as ac,
        mock.patch("lies.etl.stages.write.qmd_collection_add_if_missing") as m_add,
        mock.patch("lies.etl.stages.write.qmd_update") as m_update,
        mock.patch("lies.etl.stages.write.rebuild_index") as m_index,
    ):
        run_write(
            _wiki(tmp_path),
            _collection(tmp_path),
            fake_normalized,
            manifest=manifest,
            force=False,
        )
    ac.assert_not_called()
    m_add.assert_not_called()
    m_update.assert_not_called()
    m_index.assert_not_called()


def test_run_write_does_not_roll_back_commit_on_rebuild_index_value_error(
    tmp_path: Path,
) -> None:
    """A ValueError from rebuild_index (e.g., malformed frontmatter) must NOT
    propagate out of run_write — the wiki git commit has already happened
    and cannot be unwound by a fire-and-forget post-commit hook. The same
    guarantee applies to OSError; the previous narrow OSError-only catch
    let any non-OSError exception unwrap and abort the pipeline."""
    wiki = _wiki(tmp_path)
    (wiki.raw_dir / "cpython").mkdir(parents=True, exist_ok=True)
    manifest = mock.Mock()
    manifest.compare.return_value = False
    fake_normalized = [("x.md", "# body")]
    with (
        mock.patch("lies.etl.stages.write.atomic_commit") as ac,
        mock.patch("lies.etl.stages.write.qmd_collection_add_if_missing"),
        mock.patch("lies.etl.stages.write.qmd_update"),
        mock.patch(
            "lies.etl.stages.write.rebuild_index",
            side_effect=ValueError("bad frontmatter"),
        ),
    ):
        result = run_write(
            wiki,
            _collection(tmp_path),
            fake_normalized,
            manifest=manifest,
            force=False,
        )
    assert result.success == ["x.md"]
    ac.assert_called_once()


def test_scrape_uses_bespoke_scraper_via_scraper_cmd(tmp_path: Path) -> None:
    """When Collection.scraper_cmd is set, run_scrape imports and uses it."""
    # Create a module file that defines a BaseScraper subclass.
    mod_path = tmp_path / "bespoke_scraper.py"
    mod_path.write_text(
        "from lies.scrapers.base import BaseScraper, ParsedDoc\n"
        "class _B(BaseScraper):\n"
        "    def fetch(self, source):\n"
        "        return b''\n"
        "    def parse(self, raw):\n"
        "        return [ParsedDoc(path='x.md', content=b'hi', source_sha256='h', source_format='markdown')]\n"
        "    def emit_manifest(self, docs, raw_dir):\n"
        "        (raw_dir / 'x.md').write_bytes(b'hi')\n"
        "        return raw_dir / 'manifest.json'\n"
        "SCRAPER = _B()\n",
        encoding="utf-8",
    )
    sys.path.insert(0, str(tmp_path))
    try:
        c = Collection(
            name="bespoke",
            path=tmp_path / "raw" / "bespoke",
            source="",
            tags=[],
            scraper_cmd=f"{mod_path}:SCRAPER",
            doc_path=None,
            mapper_model=None,
            language=None,
            version="1.0.0",
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
            config={},
        )
        result = run_scrape(_wiki(tmp_path), c)
        assert result.success == ["x.md"]
        assert result.parsed_docs[0].path == "x.md"
    finally:
        sys.path.pop(0)
        sys.modules.pop("bespoke_scraper", None)


def test_scrape_scraper_cmd_import_failure_raises_scraper_unavailable(tmp_path: Path) -> None:
    from lies.scrapers.errors import ScraperUnavailable

    c = Collection(
        name="missing",
        path=tmp_path,
        source="",
        tags=[],
        scraper_cmd="nonexistent.module:thing",
        doc_path=None,
        mapper_model=None,
        language=None,
        version="1.0.0",
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
        config={},
    )
    with pytest.raises(ScraperUnavailable):
        run_scrape(_wiki(tmp_path), c)
