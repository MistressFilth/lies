"""GitHubScraper — clones repo (sparse or full) and parses markdown files.

``emit_manifest`` writes to the raw workspace; the SCRAPE stage routes
the manifest to ``wiki.cache_root / "collections" / <name> / "manifest.json"``
under the XDG cache root.
"""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tarfile
import tempfile
from pathlib import Path

from lies.scrapers.base import BaseScraper, ParsedDoc
from lies.scrapers.errors import ScraperFetchFailed, ScraperParseError


class GitHubScraper(BaseScraper):
    def fetch(self, source: str | Path) -> bytes:
        url = str(source)
        with tempfile.TemporaryDirectory(prefix="lies-gh-") as tmp:
            cmd = ["gh", "repo", "clone", url, tmp, "--", "--depth=1"]
            proc = subprocess.run(cmd, capture_output=True, check=False)
            if proc.returncode != 0:
                raise ScraperFetchFailed(f"gh clone failed: {proc.stderr.decode()}")
            return self._tar_dir(Path(tmp))

    def _tar_dir(self, root: Path) -> bytes:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for p in root.rglob("*"):
                if p.is_file():
                    tar.add(p, arcname=p.relative_to(root))
        return buf.getvalue()

    def parse(self, raw: bytes) -> list[ParsedDoc]:
        docs: list[ParsedDoc] = []
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r") as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                if not member.name.endswith((".md", ".mdx", ".rst")):
                    continue
                f = tar.extractfile(member)
                if f is None:
                    continue
                content = f.read()
                fmt = "markdown" if member.name.endswith((".md", ".mdx")) else "rst"
                docs.append(
                    ParsedDoc(
                        path=member.name,
                        content=content,
                        source_sha256=hashlib.sha256(content).hexdigest(),
                        source_format=fmt,
                    )
                )
        if not docs:
            raise ScraperParseError("no markdown/rst files found in repo")
        return docs

    def emit_manifest(self, docs: list[ParsedDoc], raw_dir: Path) -> Path:
        raw_dir.mkdir(parents=True, exist_ok=True)
        out = raw_dir / "manifest.json"
        payload = {"files": [{"path": d.path, "sha256": d.source_sha256} for d in docs]}
        out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return out
