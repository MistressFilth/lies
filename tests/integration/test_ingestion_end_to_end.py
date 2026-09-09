"""Integration test for the XDG-routed ingestion pipeline.

Asserts that ``lies sync`` lands the mirror file under the library
singleton (Phase-2 retargeting, Task 11) at
``$XDG_DATA_HOME/lies/library/collections/<c>/<slug>.md``. Wiki still
hosts the collection YAML + flock + qmd-side resolution; the write
target moved.

The test uses a name-based wiki and exercises ``lies sync <c>`` end-to-end.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest import mock

import pytest
import yaml
from typer.testing import CliRunner

from lies import xdg
from lies.cli import app
from lies.wiki.wiki import Wiki

pytestmark = pytest.mark.integration


def _git_init(path: Path) -> None:
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(path)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "t@e.com"], cwd=path, check=True, capture_output=True
    )
    subprocess.run(["git", "config", "user.name", "T"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)


def test_full_pipeline_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "end2end"
    # The CLI reads from LIES_WIKI_NAME; pin all XDG envs so the resolved
    # wiki lives entirely under tmp_path (test hermeticity).
    monkeypatch.setenv("LIES_WIKI_NAME", name)
    monkeypatch.setenv("LIES_XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("LIES_XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("LIES_XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("LIES_XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("LIES_XDG_RUNTIME_DIR", str(tmp_path / "runtime"))

    wiki = Wiki(
        name=name,
        data_root=xdg.data_home() / "lies" / name,
        config_root=xdg.config_home() / "lies" / name,
        cache_root=xdg.cache_home() / "lies" / name,
        state_root=xdg.state_home() / "lies" / name,
        runtime_root=xdg.runtime_dir_for(name),
    )
    for root in (
        wiki.data_root,
        wiki.config_root,
        wiki.cache_root,
        wiki.state_root,
        wiki.runtime_root,
    ):
        root.mkdir(parents=True, exist_ok=True)
    wiki.raw_dir.mkdir(parents=True, exist_ok=True)
    wiki.wiki_dir.mkdir(parents=True, exist_ok=True)
    (wiki.config_root / "schema.md").write_text("# schema\n", encoding="utf-8")
    (wiki.collections_dir).mkdir(parents=True, exist_ok=True)
    # load_collection (still pre-XDG) reads from ``<wiki_root>/.lies/collections``
    # for now; mirror the YAML there so the CLI's bootstrap path resolves it.
    (wiki.data_root / ".lies" / "collections").mkdir(parents=True, exist_ok=True)
    # ``data_root`` is a git repo for atomic_commit; we just need an initial
    # commit so the working tree is clean. ``git commit --allow-empty`` works
    # even when there is nothing staged yet (the test seed runs after git init).
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(wiki.data_root)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(wiki.data_root), "config", "user.email", "t@e.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(wiki.data_root), "config", "user.name", "T"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(wiki.data_root), "add", "."],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(wiki.data_root), "commit", "--allow-empty", "-m", "init"],
        check=True,
        capture_output=True,
    )

    cfg = {
        "name": "sample",
        "path": "./raw/sample",
        "source": "https://example.com/llms-full.txt",
        "tags": ["test"],
        "scraper_cmd": None,
        "doc_path": None,
        "mapper_model": None,
        "language": None,
        "version": "1.0.0",
        "created_at": "2026-08-01T00:00:00Z",
        "updated_at": "2026-08-01T00:00:00Z",
    }
    (wiki.collections_dir / "sample.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    (wiki.data_root / ".lies" / "collections" / "sample.yaml").write_text(
        yaml.safe_dump(cfg), encoding="utf-8"
    )

    canned = (
        b"# Doc 1\n"
        b"Line one\n"
        b"Line two\n"
        b"Line three\n"
        b"Line four\n"
        b"Line five\n"
        b"Line six\n"
        b"Line seven\n"
        b"\n# Doc 2\n"
        b"Line one\n"
        b"Line two\n"
        b"Line three\n"
        b"Line four\n"
        b"Line five\n"
        b"Line six\n"
        b"Line seven\n"
    )

    def fake_urlopen(req):
        resp = mock.MagicMock()
        resp.read.return_value = canned
        # WebScraper.fetch compares resp.geturl() with the requested URL to
        # reject responses that followed a redirect. The mock must echo the
        # request URL so the candidate is treated as a non-redirect response.
        resp.geturl.return_value = req.full_url
        resp.__enter__.return_value = resp
        return resp

    # Minor 50 anti-tautology: assert the production code resolves through
    # ``WebScraper.fetch`` (which calls ``urllib.request.urlopen``),
    # NOT some other code path the mock would silently miss. ``pick_scraper``
    # selects ``WebScraper`` for any ``https://...`` URL — pinning that
    # resolution makes the ``urlopen`` mock a meaningful assertion rather
    # than a placeholder.
    from lies.scrapers.base import pick_scraper
    from lies.scrapers.web import WebScraper

    resolved = pick_scraper("https://example.com/llms-full.txt")
    assert isinstance(resolved, WebScraper), (
        f"urlopen mock targets urllib.request.urlopen; production code "
        f"must go through WebScraper.fetch → urlopen. Got {type(resolved).__name__}."
    )

    # Library (Phase-2 write target) holds the mirror file under
    # ``$XDG_DATA_HOME/lies/library/collections/<c>/<slug>.md``. The wiki's
    # ``wiki_dir`` no longer receives the sync output. Initialise the
    # library's git repo before the first sync so ``LibraryWriter``'s
    # ``atomic_commit`` has somewhere to land.
    from lies.library.paths import Library

    Library.open.cache_clear()
    lib = Library.open()
    lib.git_root.mkdir(parents=True, exist_ok=True)
    (lib.git_root / ".gitkeep").write_text("")
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(lib.git_root)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(lib.git_root), "config", "user.email", "t@e.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(lib.git_root), "config", "user.name", "T"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(lib.git_root), "add", "."],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(lib.git_root), "commit", "-m", "init"],
        check=True,
        capture_output=True,
    )

    with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result1 = CliRunner().invoke(app, ["sync", "sample"])
    assert result1.exit_code == 0

    assert (lib.collections_root / "sample" / "chunk-0000.md").exists()

    # Idempotency contract (Task 11 fix #2): a second sync of an unchanged
    # source must exit 0. The mirror already exists with the same hash, so
    # ``_process_item`` short-circuits as ``mirror-collision:up_to_date``
    # (skip, not error). No ``--force`` mask — that would hide the regression
    # where any re-run exited 1 because the collision branch always bumped
    # ``result.errors``. Mismatched hashes still surface as errors.
    with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result2 = CliRunner().invoke(app, ["sync", "sample"])
    assert result2.exit_code == 0

    # Best-effort cleanup; the CWD-relative raw path the test seeded is
    # at the project root (see sync_helper) so scrub it on the way out.
    # Minor 48: previously this used ``Path.cwd() / "raw"`` which
    # depended on the test runner's working directory. With the XDG
    # envs pinned above the seeded ``raw/`` lives under the wiki's
    # data_root (XDG_DATA_HOME/lies/end2end/raw), not the project root.
    # The old cleanup silently missed the seed and left it behind for
    # subsequent runs. ``shutil.rmtree(wiki.raw_dir, ignore_errors=True)``
    # is hermetic regardless of cwd.
    if wiki.raw_dir.exists():
        shutil.rmtree(wiki.raw_dir, ignore_errors=True)
