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
        return _FakeResp(responses[req.full_url], req.full_url)

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
    docs = [
        ParsedDoc(path="x.md", content=b"# x", source_sha256=_sha("# x"), source_format="markdown")
    ]
    out = WebScraper().emit_manifest(docs, tmp_path)
    assert json.loads(out.read_text(encoding="utf-8"))["files"][0]["path"] == "x.md"


# ---------------------------------------------------------------------------
# llms.txt fetch robustness — covers sites where the canonical file is not at
# the obvious path. Repro for code.claude.com (llms-full.txt 302s to marketing;
# llms.txt lives at the host root, not the docs subdir).
# ---------------------------------------------------------------------------


def test_web_scraper_accepts_direct_llms_txt_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Source already points at llms.txt -- fetch it directly, no path append."""
    urls_called: list[str] = []
    body_text = "# Claude Code Docs\n\n- [a](https://x): d\n"

    def fake_urlopen(req):
        urls_called.append(req.full_url)
        return _FakeResp(body_text, req.full_url)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    body = WebScraper().fetch("https://code.claude.com/llms.txt")
    assert body.decode("utf-8") == body_text
    # Only the source URL should be hit -- no path-suffix trickery.
    assert urls_called == ["https://code.claude.com/llms.txt"]


def test_web_scraper_accepts_direct_llms_full_txt_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    urls_called: list[str] = []
    body_text = "FULL BODY"

    def fake_urlopen(req):
        urls_called.append(req.full_url)
        return _FakeResp(body_text, req.full_url)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    body = WebScraper().fetch("https://example.com/llms-full.txt")
    assert body.decode("utf-8") == body_text
    assert urls_called == ["https://example.com/llms-full.txt"]


def test_web_scraper_walks_up_path_to_find_llms_txt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Source points deep into a docs path. Walk parent dirs until llms.txt exists.

    Repro: user pastes ``https://code.claude.com/docs/en/overview``;
    canonical ``llms.txt`` is at ``https://code.claude.com/llms.txt``.
    """
    urls_called: list[str] = []

    def fake_urlopen(req):
        urls_called.append(req.full_url)
        # Only the host-root llms.txt actually exists; every other path 404s.
        if req.full_url == "https://code.claude.com/llms.txt":
            return _FakeResp("# Index\n\n- [a](u): d\n", req.full_url)
        raise HTTPError(req.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    body = WebScraper().fetch("https://code.claude.com/docs/en/overview")
    assert body.startswith(b"# Index")
    # Each candidate tried, deepest first, walking up. Final match is host root.
    expected = [
        "https://code.claude.com/docs/en/overview/llms-full.txt",
        "https://code.claude.com/docs/en/overview/llms.txt",
        "https://code.claude.com/docs/en/llms-full.txt",
        "https://code.claude.com/docs/en/llms.txt",
        "https://code.claude.com/docs/llms-full.txt",
        "https://code.claude.com/docs/llms.txt",
        "https://code.claude.com/llms-full.txt",
        "https://code.claude.com/llms.txt",
    ]
    assert urls_called == expected


def test_web_scraper_falls_back_when_full_redirects_away(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """llms-full.txt 302s to an unrelated URL -- treat as 'does not exist' and
    try llms.txt next. Repro: code.claude.com sends 302 to a marketing page."""
    urls_called: list[str] = []

    def fake_urlopen(req):
        urls_called.append(req.full_url)
        if req.full_url == "https://code.claude.com/llms-full.txt":
            # urllib follows the 302 and ``resp.url`` reflects the final URL.
            return _FakeResp(
                "<!doctype html><html>marketing</html>",
                "https://www.claude.com/product/claude-code",
            )
        if req.full_url == "https://code.claude.com/llms.txt":
            return _FakeResp("# Index\n", req.full_url)
        raise HTTPError(req.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    body = WebScraper().fetch("https://code.claude.com")
    assert body == b"# Index\n"
    assert any(u.endswith("/llms-full.txt") for u in urls_called)
    assert any(u.endswith("/llms.txt") for u in urls_called)


def test_web_scraper_rejects_html_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 200 with an HTML body is not an llms.txt -- fall through to next candidate."""
    urls_called: list[str] = []

    def fake_urlopen(req):
        urls_called.append(req.full_url)
        if req.full_url.endswith("/llms-full.txt"):
            return _FakeResp("<!doctype html><html>...</html>", req.full_url)
        if req.full_url.endswith("/llms.txt"):
            return _FakeResp("# Index\n", req.full_url)
        raise HTTPError(req.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    body = WebScraper().fetch("https://example.com")
    assert body == b"# Index\n"


def test_web_scraper_raises_when_no_candidate_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All candidates either redirect away or return HTML -- ScraperFetchFailed."""

    def fake_urlopen(req):
        return _FakeResp("<!doctype html><html>x</html>", req.full_url)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(ScraperFetchFailed):
        WebScraper().fetch("https://example.com")


def test_web_scraper_sends_browser_user_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Some docs hosts return 403 to ``Python-urllib/x.y`` and only serve
    llms.txt to a browser UA. The scraper must set a UA so the static fetch
    is not pre-filtered before the path-walk / redirect logic ever runs.
    Repro: code.claude.com.
    """
    seen_headers: list[dict[str, str]] = []

    def fake_urlopen(req):
        seen_headers.append(dict(req.headers))
        return _FakeResp("# Index\n", req.full_url)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    WebScraper().fetch("https://example.com")
    assert seen_headers, "urlopen was never called"
    for hdrs in seen_headers:
        ua = hdrs.get("User-agent") or hdrs.get("User-Agent")
        assert ua is not None, f"no User-Agent header sent: {hdrs}"
        assert "Python-urllib" not in ua, f"default Python UA leaked: {ua!r}"


class _FakeResp:
    def __init__(self, body: str | bytes, url: str) -> None:
        self._body = body.encode("utf-8") if isinstance(body, str) else body
        self.url = url

    def geturl(self) -> str:
        return self.url

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False
