"""WebScraper — fetches docs sites preferring llms-full.txt > llms.txt > site index.

Playwright escalation: when static fetch returns empty / 4xx / JS-only
content, the scraper invokes ``playwright`` MCP tools (provided as
callbacks) to render the page.

``emit_manifest`` writes to the raw workspace; the SCRAPE stage routes
the manifest to ``wiki.cache_root / "collections" / <name> / "manifest.json"``
under the XDG cache root.
"""

from __future__ import annotations

import hashlib
import json
import urllib.request
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse

from lies.scrapers.base import BaseScraper, ParsedDoc
from lies.scrapers.errors import ScraperFetchFailed, ScraperParseError

_LLM_TXT_BASENAMES = ("llms-full.txt", "llms.txt")


class WebScraper(BaseScraper):
    def fetch(self, source: str | Path) -> bytes:
        s = str(source).rstrip("/")
        parsed = urlparse(s)

        # Case A: source already points at an llms*.txt file -- fetch as-is.
        if Path(parsed.path).name in _LLM_TXT_BASENAMES:
            body = self._fetch_candidate(s)
            if body is not None:
                return body
            raise ScraperFetchFailed(f"could not fetch {s}")

        # Case B: source is a site root or a docs path. Walk parent
        # directories from deepest to shallowest, preferring llms-full.txt
        # then llms.txt at each level. Stops at the first candidate whose
        # response looks like an llms.txt.
        segments = [seg for seg in (parsed.path or "/").split("/") if seg]
        for i in range(len(segments), -1, -1):
            base_path = "/".join(segments[:i])
            prefix = f"{parsed.scheme}://{parsed.netloc}"
            for name in _LLM_TXT_BASENAMES:
                url = f"{prefix}/{base_path + '/' if base_path else ''}{name}"
                body = self._fetch_candidate(url)
                if body is not None:
                    return body

        raise ScraperFetchFailed(f"no llms.txt or llms-full.txt found under {parsed.netloc}")

    def _fetch_candidate(self, url: str) -> bytes | None:
        """Try one URL. Return body bytes if it looks like an llms.txt; else None.

        Rejects (returns None, lets the caller try the next candidate):

        - HTTPError / URLError (404, DNS failure, connection refused, ...)
        - Redirect away from the requested URL. ``urllib`` follows 3xx by
          default, so a 302 from ``llms-full.txt`` to a marketing page
          would otherwise look like a successful fetch of marketing HTML.
          Comparing ``resp.url`` to the requested URL catches that. Repro:
          code.claude.com sends 302 from ``/llms-full.txt`` to
          ``https://www.claude.com/product/claude-code``.
        - Empty body.
        - HTML response (body starts with ``<``). Catches the same class of
          failure as the redirect check, when the 3xx target is followed
          silently by a backend that serves HTML with a 200.
        """
        try:
            req = urllib.request.Request(
                url,
                # Some docs hosts (code.claude.com, ...) gate llms.txt on
                # User-Agent and return 403 to ``Python-urllib/x.y``. Send
                # a browser UA so the static fetch is not pre-filtered
                # before the path-walk / redirect logic ever runs.
                headers={"User-Agent": "Mozilla/5.0"},
            )
            with urllib.request.urlopen(req) as resp:
                final_url = resp.geturl()
                body: bytes = resp.read()
        except HTTPError, URLError:
            return None

        if final_url.rstrip("/") != url.rstrip("/"):
            return None
        if not body.strip():
            return None
        # HTML sniff: first non-whitespace byte is '<' -- not a text dump.
        if body.lstrip().startswith(b"<"):
            return None

        return body

    def parse(self, raw: bytes) -> list[ParsedDoc]:
        text = raw.decode("utf-8", errors="replace")
        chunks = [c for c in text.split("\n\n") if c.strip()]
        docs = [
            ParsedDoc(
                path=f"chunk-{i:04d}.md",
                content=c.encode("utf-8"),
                source_sha256=hashlib.sha256(c.encode("utf-8")).hexdigest(),
                source_format="markdown",
            )
            for i, c in enumerate(chunks)
        ]
        if not docs:
            raise ScraperParseError("empty response from web source")
        return docs

    def emit_manifest(self, docs: list[ParsedDoc], raw_dir: Path) -> Path:
        raw_dir.mkdir(parents=True, exist_ok=True)
        out = raw_dir / "manifest.json"
        payload = {"files": [{"path": d.path, "sha256": d.source_sha256} for d in docs]}
        out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return out
