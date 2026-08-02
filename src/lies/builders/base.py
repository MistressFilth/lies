"""Builder ABC and BuilderRegistry.

A :class:`Builder` converts the bytes a scraper fetched into a list
of :class:`ParsedDoc`. Builders are pure: no LLM calls, no writes
outside the workspace, no qmd. Per-doc failures raise
:class:`BuilderError` subclasses; the NORMALIZE stage quarantines
those docs and continues.

The :class:`BuilderRegistry` is a thin map from ``source_format``
string to a builder instance. ``REGISTRY`` is the module-level
singleton populated by the format-specific builder modules and
read by the NORMALIZE stage.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from pathlib import Path

from lies.builders.errors import BuilderUnavailable
from lies.collections.record import Collection
from lies.scrapers.base import ParsedDoc


class Builder(ABC):
    """Convert fetched bytes in ``workspace`` into markdown docs."""

    @abstractmethod
    def build(self, workspace: Path, *, collection: Collection) -> list[ParsedDoc]:
        """Return one or more markdown docs derived from the workspace.

        Implementations must not call an LLM, must not write outside
        ``workspace``, and must raise :class:`BuilderError` on failure.
        """


class PassThroughBuilder(Builder):
    """A builder that emits a single doc from ``<workspace>/source.md``.

    Used for the ``markdown`` source_format: the scraper already
    produced a markdown file and the builder is a no-op.
    """

    def build(self, workspace: Path, *, collection: Collection) -> list[ParsedDoc]:
        src = workspace / "source.md"
        if not src.exists():
            raise BuilderUnavailable("markdown")
        content = src.read_bytes()
        sha = hashlib.sha256(content).hexdigest()
        return [
            ParsedDoc(
                path="source.md",
                content=content,
                source_sha256=sha,
                source_format="markdown",
            )
        ]


class BuilderRegistry:
    """Map of source_format -> Builder instance."""

    def __init__(self) -> None:
        self._by_format: dict[str, Builder] = {}

    def register(self, source_format: str, builder: Builder) -> None:
        self._by_format[source_format] = builder

    def resolve(self, source_format: str) -> Builder:
        if source_format not in self._by_format:
            raise BuilderUnavailable(source_format)
        return self._by_format[source_format]

    def formats(self) -> set[str]:
        return set(self._by_format)


REGISTRY = BuilderRegistry()
