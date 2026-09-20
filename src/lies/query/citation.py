"""Citation: a path + source discriminator for retrieved pages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class Citation:
    """A page citation with explicit source.

    ``source`` discriminates between the two physical roots the
    dispatcher reads from:

    - ``"library"``: lives under
      ``Library.collections_root/<coll>/<file>``
      (``src/lies/library/paths.py:45``).
    - ``"wiki"``: lives under ``wiki.wiki_dir``.

    Hard cutover — this field is required, no defaults. Same path from
    both roots produces two distinct Citation objects.

    ``heading_path`` (F19): the nested ATX heading path of the span
    this citation references. Populated by the orchestrator's
    ``_thread_heading_paths`` step from the claim's quote match.
    ``None`` when the citation comes from a non-F19 path.
    """

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
