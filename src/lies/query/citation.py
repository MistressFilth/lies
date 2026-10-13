"""Citation: a path + source discriminator for retrieved pages."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class Citation:
    """A page citation with explicit source."""

    path: str
    source: Literal["library", "wiki"]
    line: int | None = None
    section: str | None = None
    heading_path: list[str] | None = None


@dataclass(frozen=True)
class ClaimCitation:
    """A (claim, citation) binding emitted by the synthesizer agent.

    ``claim`` must appear verbatim as a substring of the synthesized
    answer body. ``citation_index`` is 0-based into the agent's
    ``citations`` list. ``quote`` (F19) is the verbatim text from the
    cited excerpt that supports the claim — validated to appear
    verbatim in the span body by the orchestrator. The orchestrator
    drops entries that fail any check.
    """

    claim: str
    citation_index: int
    quote: str = ""


def citations_from_paths(paths: Iterable[str], excerpts: Iterable[Any]) -> list[Citation]:
    """Turns the synthesizer's ``citations: list[str]`` into ``Citation`` objects.

    ``source`` comes from the librarian's excerpts, keyed by slug,
    because the path alone cannot say which physical root the page lives
    under. It reads ``PageExcerpt.source_kind`` rather than testing
    ``collection != "wiki"``, so a library collection that happens to be
    named ``wiki`` is still a library page. Unknown paths default to
    ``"wiki"`` — a copy of the orchestrator's pre-existing mapping, not
    a verdict on the page. See AGENTS.md.
    """
    by_slug = {excerpt.slug: excerpt for excerpt in excerpts}
    out: list[Citation] = []
    for path in paths:
        excerpt = by_slug.get(path)
        kind = getattr(excerpt, "source_kind", None)
        source: Literal["library", "wiki"] = kind if kind in ("library", "wiki") else "wiki"
        out.append(Citation(path=path, source=source))
    return out
