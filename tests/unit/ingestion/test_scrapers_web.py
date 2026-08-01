import hashlib
import json
from pathlib import Path

import pytest

from lies.scrapers.base import ParsedDoc
from lies.scrapers.errors import ScraperFetchFailed
from lies.scrapers.web import WebScraper


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def test_web_scraper_prefers_llms_full_txt(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = {
        "https://example.com/llms-full.txt": "FULL",
        "https://example.com/llms.txt": "INDEX",
    }

    def fake_urlopen(req):
        return _FakeResp(responses[req.full_url])

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    s = WebScraper()
    body = s.fetch("https://example.com")
    assert body == b"FULL"


def test_web_scraper_handles_404(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(req):
        raise ScraperFetchFailed(f"404 {req.full_url}")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(ScraperFetchFailed):
        WebScraper().fetch("https://example.com")


def test_web_scraper_parse_emits_manifest(tmp_path: Path) -> None:
    docs = [ParsedDoc(path="x.md", content=b"# x", source_sha256=_sha("# x"), source_format="markdown")]
    out = WebScraper().emit_manifest(docs, tmp_path)
    assert json.loads(out.read_text(encoding="utf-8"))["files"][0]["path"] == "x.md"


class _FakeResp:
    def __init__(self, body: str) -> None:
        self._body = body.encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False
