import hashlib
import json
from pathlib import Path
from unittest import mock

import pytest

from lies.scrapers.base import ParsedDoc
from lies.scrapers.errors import ScraperFetchFailed
from lies.scrapers.github import GitHubScraper


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def test_github_scraper_fetches_via_gh(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fake_run(cmd, **kwargs):
        Path(cmd[-1]).mkdir(parents=True, exist_ok=True)
        (Path(cmd[-1]) / "README.md").write_text("# hello", encoding="utf-8")
        return mock.Mock(returncode=0, stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    s = GitHubScraper()
    raw = s.fetch("https://github.com/o/r")
    assert raw  # tar bytes from gh


def test_github_scraper_handles_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd, **kwargs):
        # Real subprocess.run with capture_output=True returns bytes for stderr.
        return mock.Mock(returncode=1, stderr=b"auth failed")

    monkeypatch.setattr("subprocess.run", fake_run)
    with pytest.raises(ScraperFetchFailed):
        GitHubScraper().fetch("https://github.com/o/r")


def test_github_scraper_parse_emits_manifest(tmp_path: Path) -> None:
    s = GitHubScraper()
    docs = [
        ParsedDoc(path="README.md", content=b"# x", source_sha256=_sha("# x"), source_format="markdown"),
    ]
    out = s.emit_manifest(docs, tmp_path)
    assert out.exists()
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["files"][0]["path"] == "README.md"