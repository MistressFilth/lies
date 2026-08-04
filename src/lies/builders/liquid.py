"""Liquid source-format builder.

Reads a single ``source.liquid`` (or pre-rendered ``source.html``) from
the workspace. If ``Collection.config["render_cmd"]`` is set, invoke the
callable to render Liquid to HTML; otherwise pass through. Pandoc handles
HTML→markdown.

The ``render_cmd`` import path (``module:attr`` or ``path.py:attr``)
mirrors ``Collection.scraper_cmd``. Callers wire up ``shopify theme
render`` or any other renderer as a Python callable.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from pathlib import Path

from lies.builders.base import REGISTRY, Builder
from lies.builders.errors import BuilderFetchFailed
from lies.collections.record import Collection
from lies.etl.normalize.pandoc_daemon import PandocDaemon
from lies.scrapers.base import ParsedDoc

RenderCmd = Callable[[bytes, dict], bytes]


def _resolve_render_cmd(spec: str) -> RenderCmd:
    """Load a ``module:attr`` callable returning HTML bytes.

    Raises ``BuilderFetchFailed`` on any malformed spec, missing module,
    or non-callable attribute. The caller is responsible for quarantining.
    """
    if ":" not in spec:
        raise BuilderFetchFailed(
            "liquid",
            f"render_cmd must be module:attr, got {spec!r}",
        )
    mod_name, attr_path = spec.split(":", 1)
    try:
        module = importlib.import_module(mod_name)
    except Exception as exc:  # ImportError, ModuleNotFoundError, SyntaxError
        raise BuilderFetchFailed("liquid", f"cannot import {mod_name!r}: {exc}") from exc
    target = getattr(module, attr_path, None)
    if not callable(target):
        raise BuilderFetchFailed(
            "liquid",
            f"{spec} is not callable (got {type(target).__name__})",
        )
    return target


def _read_source(workspace: Path) -> bytes:
    """Read source.liquid or source.html; whichever exists."""
    for name in ("source.liquid", "source.html"):
        path = workspace / name
        if path.exists():
            return path.read_bytes()
    raise BuilderFetchFailed(
        "liquid",
        f"source.liquid or source.html missing at {workspace}",
    )


def _render_html(
    source: bytes,
    collection: Collection,
    workspace: Path,
) -> bytes:
    """Render Liquid to HTML via the configured render_cmd (or passthrough)."""
    config = collection.config or {}
    spec = config.get("render_cmd")
    if not spec:
        return source
    render = _resolve_render_cmd(spec)
    context = config.get("context") or {}
    try:
        html = render(source, context)
    except Exception as exc:
        raise BuilderFetchFailed("liquid", f"render_cmd {spec!r} failed: {exc}") from exc
    if not isinstance(html, (bytes, bytearray)):
        raise BuilderFetchFailed(
            "liquid",
            f"render_cmd {spec!r} must return bytes, got {type(html).__name__}",
        )
    return bytes(html)


def _convert_html_to_markdown(html: bytes) -> bytes:
    try:
        return PandocDaemon().convert(html, "html")
    except Exception as exc:
        raise BuilderFetchFailed("pandoc", str(exc)) from exc


class LiquidBuilder(Builder):
    """Convert a single Liquid template to markdown."""

    def build(self, workspace: Path, *, collection: Collection) -> list[ParsedDoc]:
        source = _read_source(workspace)
        html = _render_html(source, collection, workspace)
        md = _convert_html_to_markdown(html)
        return [
            ParsedDoc(
                path="index.md",
                content=md,
                source_sha256=__import__("hashlib").sha256(md).hexdigest(),
                source_format="markdown",
            )
        ]


REGISTRY.register("liquid", LiquidBuilder())
