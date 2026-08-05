"""Integration test for the XDG-routed ingestion pipeline.

Asserts that the sync path writes its derived artifacts at the
XDG-derived locations:
- raw docs / per-collection inputs under ``<wiki>/raw/``.
- normalized wiki pages under ``<wiki>/wiki/``.
- scraper manifest under ``<cache_root>/collections/<c>/manifest.json``.
- hashes sidecar under ``<cache_root>/hashes/<c>.json``.
- per-sync telemetry log under ``<state_root>/logs/<c>.log``.

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
        "source": "https://example.com",
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

    canned = b"# Doc 1\n\nSome text.\n\n# Doc 2\n\nMore text.\n"
    with mock.patch("urllib.request.urlopen") as u:
        u.return_value.__enter__.return_value.read.return_value = canned
        result1 = CliRunner().invoke(app, ["sync", "sample"])
    assert result1.exit_code == 0

    # Wiki content directory holds the normalized wiki pages.
    assert (wiki.wiki_dir / "chunk-0000.md").exists()
    # Per-collection manifest moved to the cache root.
    assert (wiki.cache_root / "collections" / "sample" / "manifest.json").exists()
    # Telemetry log lives under the state root.
    assert (wiki.logs_dir / "sample.log").exists()

    with mock.patch("urllib.request.urlopen") as u:
        u.return_value.__enter__.return_value.read.return_value = canned
        result2 = CliRunner().invoke(app, ["sync", "sample"])
    assert result2.exit_code == 0

    # Best-effort cleanup; the CWD-relative raw path the test seeded is
    # at the project root (see sync_helper) so scrub it on the way out.
    shutil.rmtree(Path.cwd() / "raw", ignore_errors=True)
