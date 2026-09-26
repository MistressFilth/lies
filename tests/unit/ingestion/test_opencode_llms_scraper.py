"""Unit tests for the bespoke ``opencode_llms_scraper`` at
``$XDG_DATA_HOME/lies/scripts/opencode_llms_scraper.py``.

The scraper subclasses :class:`WebScraper` so the bulk of the
contract (index parsing, redirect / empty-body / HTML rejection,
slug derivation, manifest emission) is inherited. These tests
focus on the divergence point: per-page fetches must advertise
``Accept: text/markdown`` so the OpenCode V2 server's content
negotiation returns markdown instead of HTML.

The bespoke scraper is loaded via ``importlib.util.spec_from_file_location``
matching the production :func:`lies.library.fetcher._load_bespoke_scraper`
loader. Tests skip when the file is not present on disk (e.g. CI
without the user's local scripts directory).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from lies.scrapers.base import BaseScraper
from lies.scrapers.web import WebScraper

# Canonical XDG path for user-managed bespoke scrapers. The
# production loader accepts any path; this is the convention the
# existing ``claude_code_md_scraper.py`` already follows.
SCRAPER_PATH = Path.home() / ".local" / "share" / "lies" / "scripts" / "opencode_llms_scraper.py"

_INDEX_BODY = (
    "# OpenCode V2 Docs\n\n"
    "- [Intro](https://opencode.ai/v2/docs/): landing page\n"
    "- [Config](https://opencode.ai/v2/docs/config/): config reference\n"
)


class _FakeResp:
    """Mimics ``http.client.HTTPResponse`` enough for the scraper's urlopen().

    Supports both ``with urlopen(req) as resp:`` (context manager) and
    the ``resp.geturl()`` / ``resp.read()`` accessors the scraper uses.
    """

    def __init__(self, body: bytes | str, url: str) -> None:
        self._body = body.encode("utf-8") if isinstance(body, str) else body
        self.url = url

    def geturl(self) -> str:
        return self.url

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a) -> bool:
        return False


@pytest.fixture
def opencode_scraper() -> BaseScraper:
    """Load the bespoke scraper from its on-disk path.

    Mirrors the production :func:`_load_bespoke_scraper` loader's
    ``spec_from_file_location`` call so a regression in either path
    surfaces the same way. Returns the module-level ``scraper``
    instance — the same handle the production ``scraper_cmd`` attr
    resolves to.
    """
    if not SCRAPER_PATH.exists():
        pytest.skip(f"bespoke scraper not installed at {SCRAPER_PATH}")
    spec = importlib.util.spec_from_file_location("opencode_llms_scraper", SCRAPER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    scraper = module.scraper
    assert isinstance(scraper, WebScraper), (
        f"expected WebScraper subclass, got {type(scraper).__name__}"
    )
    return scraper


def test_opencode_llms_scraper_is_a_web_scraper_subclass(opencode_scraper) -> None:
    """The bespoke scraper must inherit WebScraper so it picks up
    the index-parsing, slug-derivation, and manifest-emission
    machinery for free."""
    assert isinstance(opencode_scraper, WebScraper)


def test_opencode_llms_scraper_sends_accept_text_markdown_on_page_fetches(
    opencode_scraper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-page fetches must advertise ``Accept: text/markdown``.

    The V2 server returns HTML for a default request and markdown
    only when the request advertises markdown in the Accept
    header. Without this, every page would be rejected by the
    inherited HTML sniff.
    """
    seen_headers: list[dict[str, str]] = []
    seen_urls: list[str] = []

    def fake_urlopen(req, *args, **kwargs):
        seen_urls.append(req.full_url)
        seen_headers.append(dict(req.headers))
        if req.full_url == "https://opencode.ai/v2/llms.txt":
            return _FakeResp(_INDEX_BODY, req.full_url)
        return _FakeResp("# page\n\nbody\n", req.full_url)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    raw = opencode_scraper.fetch("https://opencode.ai/v2/llms.txt")
    opencode_scraper.parse(raw, source="https://opencode.ai/v2/llms.txt")

    # The index fetch (one call) plus the two indexed pages = 3 calls.
    assert len(seen_urls) >= 3
    page_fetches = [u for u in seen_urls if u != "https://opencode.ai/v2/llms.txt"]
    assert page_fetches, "no per-page fetches were made"

    # The index fetch is allowed to inherit the parent's Accept
    # (the server returns text/plain for /llms.txt regardless).
    # Per-page fetches MUST advertise markdown.
    for url in page_fetches:
        matching = [h for h, u in zip(seen_headers, seen_urls) if u == url]
        assert matching, f"no header captured for {url}"
        accept = matching[0].get("Accept") or matching[0].get("accept")
        assert accept is not None, f"no Accept header on per-page fetch {url}"
        assert "text/markdown" in accept, (
            f"per-page fetch {url} sent Accept={accept!r}, expected it to include 'text/markdown'"
        )


def test_opencode_llms_scraper_index_fetch_does_not_send_markdown_accept(
    opencode_scraper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The index fetch must NOT change Accept behavior.

    ``_fetch_candidate`` is inherited unchanged — adding
    ``Accept: text/markdown`` to it would affect every other docs
    site that publishes an ``llms.txt`` index. The bespoke
    scraper only diverges on per-page fetches.
    """
    seen_headers: list[dict[str, str]] = []

    def fake_urlopen(req, *args, **kwargs):
        seen_headers.append(dict(req.headers))
        return _FakeResp(_INDEX_BODY, req.full_url)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    opencode_scraper.fetch("https://opencode.ai/v2/llms.txt")

    assert seen_headers, "urlopen was never called"
    first = seen_headers[0]
    accept = first.get("Accept") or first.get("accept")
    # The inherited _fetch_candidate sends no Accept header at all.
    # If the bespoke scraper accidentally adds one, this fails.
    assert accept is None or "text/markdown" not in accept, (
        f"index fetch leaked Accept={accept!r}; "
        "Accept: text/markdown must only appear on per-page fetches"
    )


def test_opencode_llms_scraper_per_page_rejects_html(
    opencode_scraper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the server ignores Accept and returns HTML, the
    inherited HTML sniff must drop the page at ``_fetch_doc`` time.

    Tested via ``_fetch_doc`` directly so a regression in the
    override bypasses cleanly. The end-to-end effect (when ALL
    pages fail) is ``ScraperParseError`` from ``_parse_index`` —
    covered by the upstream ``WebScraper`` tests, not here.
    """

    def fake_urlopen(req, *args, **kwargs):
        return _FakeResp("<!doctype html><html><body>x</body></html>", req.full_url)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = opencode_scraper._fetch_doc("https://opencode.ai/v2/docs/", base=None)

    assert result is None, f"expected HTML to be rejected, got {result!r}"


def test_opencode_llms_scraper_per_page_rejects_redirect(
    opencode_scraper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A redirect on a per-page fetch must drop the page at
    ``_fetch_doc`` time (inherited behavior)."""

    def fake_urlopen(req, *args, **kwargs):
        # Page redirects to an unrelated host.
        return _FakeResp("# redirect", "https://elsewhere.example/landing")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = opencode_scraper._fetch_doc("https://opencode.ai/v2/docs/", base=None)

    assert result is None, f"expected redirect to be rejected, got {result!r}"


def test_opencode_llms_scraper_per_page_handles_404(
    opencode_scraper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTTPError on a per-page fetch must return ``None`` from
    ``_fetch_doc`` (inherited behavior)."""
    from urllib.error import HTTPError

    def fake_urlopen(req, *args, **kwargs):
        raise HTTPError(req.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = opencode_scraper._fetch_doc("https://opencode.ai/v2/docs/", base=None)

    assert result is None, f"expected 404 to be swallowed, got {result!r}"


def test_opencode_llms_scraper_index_parse_raises_when_all_pages_fail(
    opencode_scraper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: when every indexed page returns HTML, the
    parent ``_parse_index`` raises ``ScraperParseError``.

    This pins the inherited "fail loud when nothing survives"
    behavior — the bespoke scraper's job is to send the right
    Accept header so this branch doesn't fire in production.
    """
    from lies.scrapers.errors import ScraperParseError

    def fake_urlopen(req, *args, **kwargs):
        if req.full_url == "https://opencode.ai/v2/llms.txt":
            return _FakeResp(_INDEX_BODY, req.full_url)
        return _FakeResp("<!doctype html><html>x</html>", req.full_url)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    raw = opencode_scraper.fetch("https://opencode.ai/v2/llms.txt")
    with pytest.raises(ScraperParseError):
        opencode_scraper.parse(raw, source="https://opencode.ai/v2/llms.txt")


def test_opencode_llms_scraper_happy_path(
    opencode_scraper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end happy path: index fetch → per-page markdown fetch → ParsedDocs."""

    def fake_urlopen(req, *args, **kwargs):
        if req.full_url == "https://opencode.ai/v2/llms.txt":
            return _FakeResp(_INDEX_BODY, req.full_url)
        # Derive a marker from the URL so the test can verify
        # which page content landed where.
        marker = req.full_url.rstrip("/").rsplit("/", 1)[-1] or "root"
        return _FakeResp(f"# page-{marker}\n\nbody of {marker}\n", req.full_url)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    raw = opencode_scraper.fetch("https://opencode.ai/v2/llms.txt")
    docs = opencode_scraper.parse(raw, source="https://opencode.ai/v2/llms.txt")

    assert len(docs) == 2
    by_path = {d.path: d.content.decode("utf-8") for d in docs}
    # The bespoke scraper inherits ``_url_to_path`` from WebScraper.
    # Both URLs (``/v2/docs/`` and ``/v2/docs/config/``) produce slugs
    # nested under ``v2/`` (no ``/docs/<lang>/`` strip because the
    # leading segment is ``v2``, not ``docs``).
    assert all(d.source_format == "markdown" for d in docs)
    assert "v2/docs.md" in by_path, f"missing index page slug: {sorted(by_path)}"
    assert "v2/docs/config.md" in by_path, f"missing config page slug: {sorted(by_path)}"
    assert by_path["v2/docs.md"].startswith("# page-docs")
    assert by_path["v2/docs/config.md"].startswith("# page-config")
