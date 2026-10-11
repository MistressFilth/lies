"""Citation: a path + source discriminator for retrieved pages."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal


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


def citations_from_paths(paths: Iterable[str], excerpts: Iterable[Any]) -> list[Citation]:
    """Turn the synthesizer's ``citations: list[str]`` into ``Citation`` objects.

    The synthesizer agent emits paths (F19 prompt shape); every
    consumer downstream — ``_validate_claim_citations`` (which indexes
    ``citation_index`` into it), ``_thread_heading_paths``, and the
    ``## Evidence`` renderer — wants the typed envelope with its
    ``source`` discriminator. The ``source`` comes from the librarian's
    excerpts, keyed by slug, because the path alone cannot say which
    physical root the page lives under.

    Lives here rather than in ``orchestrator._call_synthesizer`` so
    ``synth._ask_impl`` can use it too. That call site previously read
    ``getattr(lib_out, "citations", [])`` — ``LibrarianOutput`` has no
    ``citations`` field, so it was ``[]`` on every success path and
    ``lib_ask`` reported prose with zero citations forever. A second
    implementation here would reintroduce the same drift; one function
    with two callers cannot.

    Unknown paths default to ``"wiki"``. That is not a verdict on the
    page: it is this function's verbatim copy of the orchestrator's
    pre-existing mapping, diffed line for line, and a wrong guess here
    is no better and no worse than the orchestrator's identical guess
    one call site over. The reasoning that once justified it — "the
    conservative choice", "the dual-source contract resolves ambiguity
    library-first" — was wrong twice over. ``"wiki"`` is not the
    conservative choice (dropping the citation would be), and the
    library-first rule argues *for* ``"library"``, so invoking it here
    inverted it. The behaviour is kept because it is the orchestrator's
    behaviour; the justification is not kept because it was never
    sound.

    The consequence is worth stating rather than hiding. On the
    orchestrator path a bogus path dies in
    ``_validate_claim_citations``. ``synth._ask_impl`` applies no
    citation validation, so a path the librarian never handed over
    survives there as ``Citation(path=..., source="wiki")`` —
    ``lib_ask`` emits model-asserted, unverified citations. ``read``
    still resolves them, which is how a caller checks.
    """
    by_slug = {excerpt.slug: excerpt for excerpt in excerpts}
    out: list[Citation] = []
    for path in paths:
        excerpt = by_slug.get(path)
        source: Literal["library", "wiki"] = (
            "library" if excerpt is not None and excerpt.collection != "wiki" else "wiki"
        )
        out.append(Citation(path=path, source=source))
    return out
