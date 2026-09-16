"""WebScraper — fetches docs sites preferring llms-full.txt > llms.txt > site index.

Playwright escalation: when static fetch returns empty / 4xx / JS-only
content, the scraper invokes ``playwright`` MCP tools (provided as
callbacks) to render the page.

``emit_manifest`` writes to the raw workspace; the SCRAPE stage routes
the manifest to ``wiki.cache_root / "collections" / <name> / "manifest.json"``
under the XDG cache root.

llms.txt (the index) vs llms-full.txt (the full dump): when the source
URL ends in ``llms.txt`` -- exactly, not ``llms-full.txt`` -- parse()
treats the body as a markdown index of doc URLs and fetches each one.
Otherwise parse() chunks the body by blank lines as before. The source
URL is the canonical signal: the body of an llms-full.txt dump also
contains ``- `` patterns (for source citations), so sniffing the body
would be ambiguous.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.request
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse

from lies.scrapers.base import BaseScraper, ParsedDoc
from lies.scrapers.errors import ScraperFetchFailed, ScraperParseError

_LLM_TXT_BASENAMES = ("llms-full.txt", "llms.txt")
# Matches `- [Title](url): description` and `- [Title](url) - description`
# lines in an llms.txt index. Some llms.txt publishers (e.g. platform.claude.com)
# emit a dash separator instead of a colon; both forms are valid.
# Captures title (1), url (2), and description (3); description may be empty.
_LLMS_LINK_RE = re.compile(
    r"^\s*-\s*\[([^\]]+)\]\(([^)]+)\)\s*(?:[:\-]\s+(.*?))?\s*$",
    re.MULTILINE,
)


class WebScraper(BaseScraper):
    def __init__(self) -> None:
        self._last_resolved_url: str | None = None

    def fetch(self, source: str | Path) -> bytes:
        s = str(source).rstrip("/")
        parsed = urlparse(s)

        # Case A: source already points at an llms*.txt file -- fetch as-is.
        if Path(parsed.path).name in _LLM_TXT_BASENAMES:
            body = self._fetch_candidate(s)
            if body is not None:
                self._last_resolved_url = s
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
                    self._last_resolved_url = url
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
        except (HTTPError, URLError):
            return None

        if final_url.rstrip("/") != url.rstrip("/"):
            return None
        if not body.strip():
            return None
        # HTML sniff: first non-whitespace byte is '<' -- not a text dump.
        if body.lstrip().startswith(b"<"):
            return None

        return body

    def _fetch_doc(self, url: str, base: str | None = None) -> bytes | None:
        """Fetch a single doc URL referenced from an llms.txt index.

        Same redirect / HTML / error semantics as ``_fetch_candidate`` but
        a higher timeout is acceptable (these are real doc pages, not
        50-byte index probes).

        ``base`` is the source URL (the llms.txt manifest URL);
        relative link targets (e.g. ``/docs/foo.md``) are resolved
        against it via :func:`urllib.parse.urljoin` before fetching.
        Without that resolution, ``urllib.request.Request`` rejects
        non-absolute URLs with ``ValueError: unknown url type``.
        """
        from urllib.parse import urljoin

        resolved = urljoin(base, url) if base else url
        try:
            req = urllib.request.Request(resolved, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                final_url = resp.geturl()
                body: bytes = resp.read()
        except (HTTPError, URLError, TimeoutError, OSError, ValueError):
            return None

        if final_url.rstrip("/") != resolved.rstrip("/"):
            return None
        if not body.strip():
            return None
        if body.lstrip().startswith(b"<"):
            return None
        return body

    @staticmethod
    def _url_to_path(url: str, idx: int, used: set[str]) -> str:
        """Derive a unique relative path that mirrors the source URL's hierarchy.

        Strips a leading ``/docs/<lang>/`` site prefix when present (so
        ``https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices.md``
        becomes ``agents-and-tools/agent-skills/best-practices.md``).
        Falls back to ``doc-NNNN.md`` for paths that have no useful tail.
        Appends an index suffix for any remaining collisions.
        """
        p = urlparse(url)
        parts = [seg for seg in p.path.split("/") if seg]
        # Strip site-prefix: ``/docs/<lang>/...`` → ``...``
        if len(parts) >= 2 and parts[0] == "docs" and len(parts[1]) <= 5:
            parts = parts[2:]
        if not parts:
            name = f"doc-{idx:04d}.md"
        else:
            *dirs, tail = parts
            if not tail.endswith((".md", ".mdx", ".markdown")):
                tail = f"{tail}.md"
            name = "/".join(dirs + [tail]) if dirs else tail
        # Last-resort uniqueness via index suffix on the tail.
        n = 1
        while name in used:
            if "/" in name:
                dirs, tail = name.rsplit("/", 1)
            else:
                dirs, tail = "", name
            stem = tail.rsplit(".", 1)[0]
            suffix = tail.rsplit(".", 1)[-1]
            suffixed = f"{stem}-{n}.{suffix}"
            name = f"{dirs}/{suffixed}" if dirs else suffixed
            n += 1
        return name

    @staticmethod
    def _extract_llms_links(text: str) -> list[tuple[str, str]]:
        """Parse ``- [Title](url): description`` lines from an llms.txt body."""
        return [(m.group(1).strip(), m.group(2).strip()) for m in _LLMS_LINK_RE.finditer(text)]

    def parse(self, raw: bytes, *, source: str | Path | None = None) -> list[ParsedDoc]:
        text = raw.decode("utf-8", errors="replace")

        # Decide mode from the SOURCE URL, not the body. The body of an
        # llms-full.txt dump also contains ``- `` patterns (source
        # citations), so sniffing the body would misclassify full dumps
        # as indexes.
        resolved = self._last_resolved_url or (str(source) if source else None)
        basename = Path(urlparse(resolved).path).name if resolved else None
        is_index = basename == "llms.txt" and basename != "llms-full.txt"

        if is_index:
            return self._parse_index(text, source=source)

        return self._parse_chunked(text)

    def _parse_index(self, text: str, *, source: str | Path | None = None) -> list[ParsedDoc]:
        """Follow every doc URL listed in the llms.txt index, return one ParsedDoc per fetched page.

        Pages whose fetch fails (404, redirect, HTML, timeout,
        relative URL with no base) are dropped silently -- partial
        ingestion is better than a hard failure when the index lists
        70+ pages.

        The earlier implementation appended a ``_index.md``
        ``ParsedDoc`` for the index body itself ("table of contents
        survives for downstream reference"). No consumer of that
        emission ever materialised; the library's slug regex
        (``^[a-z0-9]...``) rejects underscore-prefixed stems, so
        emitting ``_index.md`` forced a ``SlugError`` that propagated
        uncaught out of ``_process_item`` and aborted every
        ``lies ingest --source <llms.txt URL>`` run on item #0. The
        emission was dropped; downstream stages that want the index
        contents can read the source URL themselves.

        Relative link handling: many llms.txt publishers emit
        ``/docs/path/page.md`` (path-only) rather than the full URL.
        Such links are resolved against the ``source`` URL via
        :func:`urllib.parse.urljoin` before fetching; without that
        resolution, ``urllib.request.Request(url)`` raises
        ``ValueError: unknown url type`` and every page drops.
        """
        docs: list[ParsedDoc] = []
        used: set[str] = set()

        base_str = str(source) if source is not None else (self._last_resolved_url or None)

        links = self._extract_llms_links(text)
        for idx, (_title, url) in enumerate(links):
            body = self._fetch_doc(url, base=base_str)
            if body is None:
                continue
            path = self._url_to_path(url, idx, used)
            used.add(path)
            docs.append(
                ParsedDoc(
                    path=path,
                    content=body,
                    source_sha256=hashlib.sha256(body).hexdigest(),
                    source_format="markdown",
                )
            )

        if not docs:
            raise ScraperParseError("llms.txt index yielded no parseable docs")
        return docs

    def _parse_chunked(self, text: str) -> list[ParsedDoc]:
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
