"""Sphinx doc-tree source-format builder.

Walks ``<workspace>/src/`` for ``.rst`` files. Honors
``collection.config``:

- ``sphinx_includes`` (default ``["**/*.rst"]``) — glob patterns to keep
- ``sphinx_excludes`` (default ``[]``) — glob patterns to drop
- ``sphinx_renames`` (default ``{}``) — relpath -> relpath

Each kept file goes through the pandoc wrapper. Output
``ParsedDoc.path`` is the rename target or the original relpath
with the ``.rst`` extension replaced by ``.md``.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from pathlib import Path

from lies.builders.base import REGISTRY, Builder
from lies.builders.errors import BuilderFetchFailed
from lies.collections.record import Collection
from lies.etl.normalize.pandoc_daemon import PandocDaemon
from lies.scrapers.base import ParsedDoc


def _matches(path: Path, includes: Iterable[str], excludes: Iterable[str]) -> bool:
    """Return True if ``path`` matches any include and no exclude."""
    include_hit = any(path.match(g) or path.match(g.lstrip("/")) for g in includes)
    exclude_hit = any(path.match(g) or path.match(g.lstrip("/")) for g in excludes)
    return include_hit and not exclude_hit


def _as_markdown(relpath: str) -> str:
    return re.sub(r"\.rst$", ".md", relpath)


class SphinxBuilder(Builder):
    def build(self, workspace: Path, *, collection: Collection) -> list[ParsedDoc]:
        cfg = collection.config or {}
        includes = cfg.get("sphinx_includes", ["**/*.rst"])
        excludes = cfg.get("sphinx_excludes", [])
        renames: dict[str, str] = cfg.get("sphinx_renames", {})
        root = workspace / "src"
        if not root.exists():
            raise BuilderFetchFailed("sphinx", f"src/ missing under {workspace}")
        out: list[ParsedDoc] = []
        for src in sorted(root.rglob("*.rst")):
            rel = src.relative_to(root).as_posix()
            if not _matches(src, includes, excludes):
                continue
            try:
                md_bytes = PandocDaemon().convert(src.read_bytes(), "rst")
            except Exception as exc:  # translate any daemon failure
                raise BuilderFetchFailed("pandoc", f"{rel}: {exc}") from exc
            md = md_bytes.decode("utf-8", errors="replace")
            md_encoded = md.encode("utf-8")
            target = renames.get(rel, _as_markdown(rel))
            out.append(
                ParsedDoc(
                    path=target,
                    content=md_encoded,
                    source_sha256=hashlib.sha256(md_encoded).hexdigest(),
                    source_format="markdown",
                )
            )
        return out


REGISTRY.register("sphinx", SphinxBuilder())
