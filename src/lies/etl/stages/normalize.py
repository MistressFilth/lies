"""NORMALIZING stage — builders + format_dispatch + obsidian.apply per doc.

For source formats with a registered builder, the stage materializes
a per-doc scratch workspace and dispatches the bytes through the
builder. The builder returns one or more ParsedDoc objects whose
content is post-build markdown. The Obsidian frontmatter pass runs
after the builder, not before.

Emits parsed_docs as a list of ParsedDoc with content replaced by the
post-normalize markdown (utf-8 encoded). Downstream stages consume
this list.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from lies.builders.base import REGISTRY
from lies.builders.errors import BuilderError
from lies.collections.record import Collection
from lies.etl.normalize import format_dispatch, obsidian
from lies.etl.normalize.format_dispatch import UnknownFormatError
from lies.scrapers.base import ParsedDoc
from lies.wiki.wiki import Wiki

if TYPE_CHECKING:
    from lies.etl.pipeline import StageResult


def _materialize(workspace: Path, fmt: str, raw: bytes) -> None:
    """Place ``raw`` at the path the builder expects for ``fmt``."""
    if fmt == "pdf":
        (workspace / "source.pdf").write_bytes(raw)
    elif fmt == "html":
        (workspace / "source.html").write_bytes(raw)
    elif fmt == "sphinx":
        (workspace / "src").mkdir(parents=True, exist_ok=True)
        (workspace / "src" / "index.rst").write_bytes(raw)


def _materialize_bespoke(workspace: Path, doc: ParsedDoc) -> None:
    """Materialize a synthetic manifest + body file for a bespoke doc.

    The bespoke builder walks ``<workspace>/manifest.json`` and reads
    ``<workspace>/<entry.path>`` for each entry. We synthesize a
    single-entry manifest pointing at the per-doc body so the builder
    behaves identically to a scraper that pre-wrote both files.
    """
    workspace.mkdir(parents=True, exist_ok=True)
    body_name = doc.path.rsplit("/", 1)[-1] or "body.md"
    (workspace / body_name).write_bytes(doc.content)
    manifest = {
        "files": [
            {
                "path": body_name,
                "out_path": doc.path,
                "source_format": "markdown",
                "sha256": doc.source_sha256,
            }
        ]
    }
    (workspace / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def _doc_title(doc: ParsedDoc) -> str:
    return doc.path.rsplit("/", 1)[-1].rsplit(".", 1)[0]


def run_normalize(wiki: Wiki, collection: Collection, docs: list[ParsedDoc]) -> StageResult:
    from lies.etl.pipeline import StageResult

    success: list[str] = []
    quarantined: list[tuple[str, str]] = []
    bytes_in = 0
    out_docs: list[ParsedDoc] = []
    wiki.scratch_dir.mkdir(parents=True, exist_ok=True)
    for doc in docs:
        bytes_in += len(doc.content)
        try:
            if doc.source_format in REGISTRY.formats() and doc.source_format != "markdown":
                with tempfile.TemporaryDirectory(dir=wiki.scratch_dir) as td:
                    workspace = Path(td)
                    if doc.source_format == "bespoke":
                        _materialize_bespoke(workspace, doc)
                    else:
                        _materialize(workspace, doc.source_format, doc.content)
                    built = REGISTRY.resolve(doc.source_format).build(
                        workspace, collection=collection
                    )
                if not built:
                    quarantined.append((doc.path, "builder produced no docs"))
                    continue
                for b in built:
                    wiki_markdown = obsidian.apply(
                        b.content.decode("utf-8", errors="replace"),
                        frontmatter={
                            "title": _doc_title(b),
                            "collection": collection.name,
                            "tags": collection.tags,
                        },
                    )
                    success.append(b.path)
                    out_docs.append(
                        ParsedDoc(
                            path=b.path,
                            content=wiki_markdown.encode("utf-8"),
                            source_sha256=b.source_sha256,
                            source_format="markdown",
                        )
                    )
                continue
            markdown = format_dispatch.dispatch(doc.content, doc.source_format)
            wiki_markdown = obsidian.apply(
                markdown,
                frontmatter={
                    "title": _doc_title(doc),
                    "collection": collection.name,
                    "tags": collection.tags,
                },
            )
            success.append(doc.path)
            out_docs.append(
                ParsedDoc(
                    path=doc.path,
                    content=wiki_markdown.encode("utf-8"),
                    source_sha256=doc.source_sha256,
                    source_format=doc.source_format,
                )
            )
        except BuilderError as exc:
            quarantined.append((doc.path, f"builder error: {exc}"))
        except UnknownFormatError as exc:
            quarantined.append((doc.path, str(exc)))
        except Exception as exc:  # noqa: BLE001 - quarantine is the catch-all
            quarantined.append((doc.path, f"normalize failed: {exc}"))
    return StageResult(
        success=success,
        quarantined=quarantined,
        skipped=[],
        parsed_docs=out_docs,
        bytes_in=bytes_in,
        bytes_out=0,
    )
