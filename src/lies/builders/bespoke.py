"""Bespoke scraper output dispatcher.

A bespoke scraper (``Collection.scraper_cmd``) is responsible for
fetching and possibly converting. Its output is a
``<workspace>/manifest.json`` file plus the doc bodies. This builder
walks the manifest:

- If ``source_format`` is ``markdown``, the doc is already converted;
  pass through unchanged.
- If ``source_format`` is registered, delegate to that builder in a
  per-doc sub-workspace.
- Otherwise (e.g. ``liquid``), pass the doc through unchanged; the
  NORMALIZE stage quarantines it.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from lies.builders.base import REGISTRY, Builder, BuilderRegistry
from lies.collections.record import Collection
from lies.scrapers.base import ParsedDoc


class BespokeBuilder(Builder):
    def __init__(self, registry: BuilderRegistry | None = None) -> None:
        self._registry = registry or REGISTRY

    def build(self, workspace: Path, *, collection: Collection) -> list[ParsedDoc]:
        manifest_path = workspace / "manifest.json"
        if not manifest_path.exists():
            return []
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        out: list[ParsedDoc] = []
        for entry in manifest.get("files", []):
            src_path = workspace / entry["path"]
            emitted = entry["source_format"]
            target = entry.get("out_path", entry["path"])
            if not src_path.exists():
                continue
            content = src_path.read_bytes()
            sha_actual = hashlib.sha256(content).hexdigest()
            if emitted == "markdown":
                out.append(
                    ParsedDoc(
                        path=target,
                        content=content,
                        source_sha256=sha_actual,
                        source_format="markdown",
                    )
                )
                continue
            try:
                builder = self._registry.resolve(emitted)
            except Exception:  # noqa: BLE001 - passthrough is intentional
                out.append(
                    ParsedDoc(
                        path=target,
                        content=content,
                        source_sha256=sha_actual,
                        source_format=emitted,
                    )
                )
                continue
            sub_token = hashlib.sha256(str(src_path).encode("utf-8")).hexdigest()[:12]
            sub = workspace / f".sub-{sub_token}"
            try:
                sub.mkdir(exist_ok=True)
                (sub / ("source." + _ext_for(emitted))).write_bytes(content)
                sub_docs = builder.build(sub, collection=collection)
            finally:
                shutil.rmtree(sub, ignore_errors=True)
            for d in sub_docs:
                if len(sub_docs) == 1:
                    out_path = d.path
                else:
                    parent = target.rsplit("/", 1)[0] if "/" in target else ""
                    filename = d.path.rsplit("/", 1)[-1]
                    out_path = f"{parent}/{filename}" if parent else filename
                out.append(
                    ParsedDoc(
                        path=out_path,
                        content=d.content,
                        source_sha256=d.source_sha256,
                        source_format="markdown",
                    )
                )
        return out


def _ext_for(fmt: str) -> str:
    return {
        "rst": "rst",
        "html": "html",
        "pdf": "pdf",
        "sphinx": "rst",
        "liquid": "liquid",
    }.get(fmt, "txt")


REGISTRY.register("bespoke", BespokeBuilder())
