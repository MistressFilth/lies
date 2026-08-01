import hashlib
import json
from pathlib import Path
from urllib.error import HTTPError

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
    """When both candidates 404, the loop must exhaust and raise ScraperFetchFailed.

    The production code's `except (HTTPError, URLError)` clause is what
    lets the loop try the second candidate. The mock must raise one of
    those exceptions so the except clause is exercised; otherwise the
    test passes by accident because the first exception propagates.
    """
    urls_called: list[str] = []

    def fake_urlopen(req):
        urls_called.append(req.full_url)
        raise HTTPError(req.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(ScraperFetchFailed):
        WebScraper().fetch("https://example.com")
    assert urls_called == [
        "https://example.com/llms-full.txt",
        "https://example.com/llms.txt",
    ]


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
