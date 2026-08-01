import hashlib
import io
import json
import tarfile
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
        # cmd shape: ["gh", "repo", "clone", url, tmp, "--", "--depth=1"]
        # cmd[-3] is the tempdir that the implementation will tar.
        tmp = Path(cmd[-3])
        tmp.mkdir(parents=True, exist_ok=True)
        (tmp / "README.md").write_text("# hello", encoding="utf-8")
        return mock.Mock(returncode=0, stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    s = GitHubScraper()
    raw = s.fetch("https://github.com/o/r")
    assert raw
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r") as tar:
        members = tar.getmembers()
    assert any(m.name == "README.md" for m in members)


def test_github_scraper_handles_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd, **kwargs):
        # Real subprocess.run with capture_output=True returns bytes for stderr.
        return mock.Mock(returncode=1, stderr=b"auth failed")

    monkeypatch.setattr("subprocess.run", fake_run)
    with pytest.raises(ScraperFetchFailed):
        GitHubScraper().fetch("https://github.com/o/r")


def test_github_scraper_parse_markdown_and_rst() -> None:
    md_body = b"# Title\n\nBody.\n"
    rst_body = b"Title\n=====\n\nBody.\n"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, body in (("README.md", md_body), ("docs/intro.rst", rst_body)):
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    docs = GitHubScraper().parse(buf.getvalue())
    assert len(docs) == 2
    by_path = {d.path: d for d in docs}
    assert by_path["README.md"].source_format == "markdown"
    assert by_path["README.md"].source_sha256 == hashlib.sha256(md_body).hexdigest()
    assert by_path["docs/intro.rst"].source_format == "rst"
    assert by_path["docs/intro.rst"].source_sha256 == hashlib.sha256(rst_body).hexdigest()


def test_github_scraper_parse_emits_manifest(tmp_path: Path) -> None:
    s = GitHubScraper()
    docs = [
        ParsedDoc(path="README.md", content=b"# x", source_sha256=_sha("# x"), source_format="markdown"),
    ]
    out = s.emit_manifest(docs, tmp_path)
    assert out.exists()
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["files"][0]["path"] == "README.md"