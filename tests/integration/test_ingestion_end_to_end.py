import subprocess
import unittest.mock as mock
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from lies.cli import app


pytestmark = pytest.mark.integration


def _git_init(path: Path) -> None:
    subprocess.run(["git", "init", "--initial-branch=main", str(path)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)


def test_full_pipeline_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / ".lies" / "collections").mkdir(parents=True)
    (wiki / "raw").mkdir()

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
    (wiki / ".lies" / "collections" / "sample.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")

    _git_init(wiki)
    monkeypatch.setenv("LIES_WIKI_ROOT", str(wiki))

    canned = b"# Doc 1\n\nSome text.\n\n# Doc 2\n\nMore text.\n"
    with mock.patch("urllib.request.urlopen") as u:
        u.return_value.__enter__.return_value.read.return_value = canned
        result1 = CliRunner().invoke(app, ["sync", "sample"])

    assert result1.exit_code == 0
    with mock.patch("urllib.request.urlopen") as u:
        u.return_value.__enter__.return_value.read.return_value = canned
        result2 = CliRunner().invoke(app, ["sync", "sample"])
    assert result2.exit_code == 0
