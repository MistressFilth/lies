"""NORMALIZING stage — format_dispatch + obsidian.apply per doc.

Emits parsed_docs as a list of ParsedDoc with content replaced by the
post-normalize markdown (utf-8 encoded). Downstream stages consume
this list.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from lies.collections.record import Collection
from lies.etl.normalize import format_dispatch, obsidian
from lies.etl.normalize.format_dispatch import UnknownFormatError
from lies.scrapers.base import ParsedDoc

if TYPE_CHECKING:
    from lies.etl.pipeline import StageResult


def run_normalize(collection: Collection, docs: list[ParsedDoc]) -> StageResult:
    from lies.etl.pipeline import StageResult

    success: list[str] = []
    quarantined: list[tuple[str, str]] = []
    bytes_in = 0
    out_docs: list[ParsedDoc] = []
    for doc in docs:
        bytes_in += len(doc.content)
        try:
            markdown = format_dispatch.dispatch(doc.content, doc.source_format)
            wiki_markdown = obsidian.apply(
                markdown,
                frontmatter={
                    "title": doc.path.rsplit("/", 1)[-1].rsplit(".", 1)[0],
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
