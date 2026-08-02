"""HTML source-format builder.

Reads ``<workspace>/source.html`` and converts to markdown via the
existing pandoc wrapper. Emits a single :class:`ParsedDoc` at
``index.md``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from lies.builders.base import REGISTRY, Builder
from lies.builders.errors import BuilderFetchFailed
from lies.collections.record import Collection
from lies.etl.normalize.pandoc_daemon import PandocDaemon
from lies.scrapers.base import ParsedDoc


class HTMLBuilder(Builder):
    """Convert a single ``source.html`` workspace into one markdown doc."""

    def build(self, workspace: Path, *, collection: Collection) -> list[ParsedDoc]:
        del collection  # HTMLBuilder does not use collection metadata.
        src = workspace / "source.html"
        if not src.exists():
            raise BuilderFetchFailed("html", f"source.html missing at {src}")
        raw = src.read_bytes()
        try:
            md_bytes = PandocDaemon().convert(raw, "html")
        except Exception as exc:  # any daemon error -> builder error
            raise BuilderFetchFailed("pandoc", str(exc)) from exc
        md = md_bytes.decode("utf-8", errors="replace")
        md_encoded = md.encode("utf-8")
        return [
            ParsedDoc(
                path="index.md",
                content=md_encoded,
                source_sha256=hashlib.sha256(md_encoded).hexdigest(),
                source_format="markdown",
            )
        ]


REGISTRY.register("html", HTMLBuilder())
